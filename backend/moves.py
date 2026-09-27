"""Bounded voice-requested manual moves, measured by the mounted phone's tracked pose.

A move is one straight forward distance or one in-place turn, stated with a unit. Speech
only proposes it (`propose_move`, backend/nav_actions.py); `/nav/confirm` starts it after a
deliberate arm in manual mode. The runner submits the same velocity commands as held-button
driving through `Motion.submit`, so the lease, watchdog, relay permits and Stop are unchanged,
and every way it ends goes through the app's `stop(reason)`. Distance and angle come only from
fresh, normally tracked poses, never from elapsed motor time. See backend/NAV_ACTIONS.md.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import math
import re
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

UNIT_M = {'cm': .01, 'm': 1., 'in': .0254}
FORWARD_M = (.05, .5)  # a short supervised indoor move; longer goes through map navigation
TURN_DEG = (10., 90.)
LIMITS_TEXT = 'one forward move of 5 to 50 centimeters, or one left or right turn of 10 to 90 degrees'


@dataclass(frozen=True)
class MoveRequest:
    direction: str  # 'forward', 'left' or 'right'
    amount: float  # as said, in `unit`
    unit: str  # 'cm', 'm', 'in' for forward; 'deg' for turns

    @property
    def turn(self) -> bool:
        return self.direction != 'forward'

    @property
    def target(self) -> float:
        """Meters forward, or radians of turn."""
        return math.radians(self.amount) if self.turn else self.amount * UNIT_M[self.unit]

    @property
    def said(self) -> str:
        return f'{self.amount:g} {self.unit}'

    @property
    def label(self) -> str:
        if self.turn:
            return f'turn {self.direction} {self.amount:g}°'
        return f'forward {self.said} ({self.target:.2f} m)'

    def describe(self) -> dict:
        return dict(direction=self.direction, amount=self.amount, unit=self.unit, label=self.label,
                    quantity='angle' if self.turn else 'distance',
                    requested=round(self.amount if self.turn else self.target, 4),
                    requested_unit='deg' if self.turn else 'm', limits=LIMITS_TEXT)


def request_problem(request: MoveRequest) -> str | None:
    """'move_out_of_range' outside the limits; a request is refused, never clamped."""
    low, high = TURN_DEG if request.turn else FORWARD_M
    value = request.amount if request.turn else request.target
    return None if low - 1e-9 <= value <= high + 1e-9 else 'move_out_of_range'


# The transcript is the ground truth for what was said; the model's typed proposal must match it.
_UNITS = {'cm': r'cm|centimet(?:er|re)s?', 'm': r"(?<!['’])m|met(?:er|re)s?", 'in': r'inch(?:es)?',
          'deg': r'deg(?:ree)?s?|°'}
_OTHER = r'mm|millimet(?:er|re)s?|km|kilomet(?:er|re)s?|feet|foot|ft|yards?'
_SMALL = ('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen '
          'sixteen seventeen eighteen nineteen').split()
_TENS = {w: 10 * i for i, w in enumerate('_ _ twenty thirty forty fifty sixty seventy eighty ninety'.split())
         if w != '_'}


def _numbers(text: str) -> set[float]:
    found = {float(n) for n in re.findall(r'\d+(?:\.\d+)?', text)}
    value = None
    for word in re.findall(r'[a-z]+', text.replace('-', ' ')):
        if word in _SMALL or word in _TENS:
            value = (value or 0) + (_SMALL.index(word) if word in _SMALL else _TENS[word])
        elif word == 'hundred':
            value = (value or 1) * 100
        elif word == 'and' and value is not None:
            continue
        else:
            if value is not None:
                found.add(float(value))
            value = None
    if value is not None:
        found.add(float(value))
    return found


def heard_problem(text, amount: float, unit: str) -> str | None:
    """'move_unclear' unless the transcript says this number and this unit, and no other unit."""
    if not isinstance(text, str):
        return 'move_unclear'
    text = re.sub(r'(\d)([a-z°])', r'\1 \2', text.lower())
    units = {name for name, pattern in _UNITS.items() if re.search(rf'(?:\b|(?<=\s))(?:{pattern})(?:\b|(?=\s|$))', text)}
    if units != {unit} or re.search(rf'\b(?:{_OTHER})\b', text) or float(amount) not in _numbers(text):
        return 'move_unclear'
    return None


@dataclass(frozen=True)
class MoveSettings:
    rate_hz: float = 10.
    pose_max_age_s: float = .25  # the health/watchdog freshness rule
    no_progress_s: float = 2.  # commanded motion without this much measured progress stops the move
    progress_m: float = .02
    progress_rad: float = .05
    lateral_max_m: float = .10  # a forward move drifting sideways further than this is not "forward"
    heading_drift_rad: float = .35
    reverse_max_m: float = .05
    wrong_turn_rad: float = .17  # turning the other way (e.g. a wrong yaw sign) stops within ~10 degrees
    pivot_travel_m: float = .25  # a pivot moves the camera on a small arc; more is not a turn in place
    settle_s: float = .6  # after the zero, keep measuring so coasting is reported
    min_timeout_s: float = 3.
    max_s: float = 20.


STOPPED_TEXT = {
    'operator_stop': 'you pressed Stop',
    'pose_stale': 'phone tracking went stale',
    'tracking_lost': 'phone tracking was lost',
    'move_diverging': 'the rover did not move in the requested direction',
    'move_no_progress': 'phone tracking showed no progress',
    'move_timeout': 'it took longer than allowed',
    'disarmed': 'the rover was disarmed',
    'command_stale': 'the arm it was confirmed under ended',
    'mode_change': 'the mode changed',
    'session_reset': 'the map was reset',
    'phone_disconnected': 'the phone disconnected',
    'car_error': 'the car link failed',
}


class MoveRunner:
    """At most one move. `result` is the latest move's state, updated while it runs.

    `pose()` is the rover pose or None; `submit(generation, mode, v, w)` is Motion.submit;
    `stop(reason)` is the app's latched stop and must call `halt(reason)`; `armed_mode()` is
    the mode while armed, else None; `speeds` are the (m/s, rad/s) magnitudes to command.
    """

    def __init__(self, settings: MoveSettings, *, pose, submit, stop, armed_mode, speeds,
                 early_stop_m: float = 0., clock=time.monotonic):
        self.settings, self.speeds, self.early_stop_m, self.clock = settings, speeds, early_stop_m, clock
        self._pose, self._submit, self._stop, self._armed_mode = pose, submit, stop, armed_mode
        self._task: asyncio.Task | None = None
        self._reason: str | None = None
        self.result: dict | None = None

    @property
    def active(self) -> bool:
        return self._task is not None

    def start(self, request: MoveRequest, generation: int, proposal_id: str) -> dict:
        if self._task is not None:
            raise RuntimeError('move_in_progress')
        self._reason = None
        self.result = dict(version=1, move_id=uuid4().hex, proposal_id=proposal_id, status='running', reason=None,
                           **request.describe(), achieved=None, measured=False, text=None)
        self._task = asyncio.create_task(self._run(request, generation))
        return dict(self.result)

    def halt(self, reason: str) -> None:
        """Called from the app's stop(); the run stops commanding at its next tick and settles."""
        if self._task is not None and self._reason is None:
            self._reason = reason

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _finish(self, reason):
        if self._reason is None:
            self._reason = reason
        self._stop(reason)  # disarms and zeroes; reaches halt(), which keeps the first reason

    async def _run(self, request, generation):
        s = self.settings
        period = 1. / s.rate_hz
        sign = -1. if request.direction == 'right' else 1.
        v, w = (0., sign * self.speeds[1]) if request.turn else (self.speeds[0], 0.)
        target = request.target
        early = 0. if request.turn else min(self.early_stop_m, target / 2)
        start = self.clock()
        deadline = start + min(s.max_s, max(s.min_timeout_s, 2 * target / (abs(w) or v) + 1.))
        origin = None  # (x, z, yaw) of the first fresh pose
        last_yaw = turned = 0.
        best, improved = 0., start
        try:
            while self._reason is None:
                now = self.clock()
                if self._armed_mode() != 'manual':
                    self._finish('disarmed')
                    break
                pose = self._fresh()
                if pose is None:
                    raw = self._pose()
                    self._finish('tracking_lost' if raw is not None and raw.tracking != 'normal'
                                 and raw.age_s <= s.pose_max_age_s else 'pose_stale')
                    break
                if origin is None:
                    origin, last_yaw = (pose.x, pose.z, pose.yaw_rad), pose.yaw_rad
                turned += math.remainder(pose.yaw_rad - last_yaw, math.tau)
                last_yaw = pose.yaw_rad
                progress, off_course = self._measure(request, origin, pose, sign * turned)
                self._record(request, progress)
                step = s.progress_rad if request.turn else s.progress_m
                if progress >= best + step:
                    best, improved = progress, now
                if progress >= target - early - 1e-6:
                    self._finish('move_complete')
                elif off_course:
                    self._finish('move_diverging')
                elif now - improved >= s.no_progress_s:
                    self._finish('move_no_progress')
                elif now >= deadline:
                    self._finish('move_timeout')
                elif not self._submit(generation, 'manual', v, w):
                    self._finish('command_stale')
                else:
                    await asyncio.sleep(period)
            # Motion is already zeroed; keep measuring briefly so coasting is reported, not hidden.
            for _ in range(max(1, round(s.settle_s * s.rate_hz))):
                pose = self._fresh()
                if pose is None or origin is None:
                    break
                turned += math.remainder(pose.yaw_rad - last_yaw, math.tau)
                last_yaw = pose.yaw_rad
                self._record(request, self._measure(request, origin, pose, sign * turned)[0])
                await asyncio.sleep(period)
        except Exception:  # a bookkeeping bug must still stop the rover
            logger.exception('move run failed')
            if self._reason is None:
                self._finish('move_error')
        finally:
            self._conclude(request)
            if self._task is asyncio.current_task():
                self._task = None

    def _fresh(self):
        pose = self._pose()
        if pose is None or pose.age_s > self.settings.pose_max_age_s or pose.tracking != 'normal':
            return None
        return pose

    def _measure(self, request, origin, pose, turned):
        """(progress toward the target, off course?): meters along the start heading, or radians
        turned toward the requested side (`turned`, accumulated tick by tick)."""
        s = self.settings
        x0, z0, yaw0 = origin
        dx, dz = pose.x - x0, pose.z - z0
        if request.turn:
            return turned, turned < -s.wrong_turn_rad or math.hypot(dx, dz) > s.pivot_travel_m
        along = dx * math.sin(yaw0) + dz * math.cos(yaw0)
        lateral = dx * math.cos(yaw0) - dz * math.sin(yaw0)
        drift = abs(math.remainder(pose.yaw_rad - yaw0, math.tau))
        return along, along < -s.reverse_max_m or abs(lateral) > s.lateral_max_m or drift > s.heading_drift_rad

    def _record(self, request, progress):
        self.result.update(achieved=round(math.degrees(progress) if request.turn else progress, 4), measured=True)

    def _conclude(self, request):
        reason = self._reason or 'move_error'
        status = 'completed' if reason == 'move_complete' else 'stopped' if reason == 'operator_stop' else 'failed'
        achieved = self.result['achieved']
        if achieved is None:
            done = 'before any measured movement'
        elif request.turn:
            done = f'{abs(achieved):.0f} degrees of the requested {request.amount:g}'
        else:
            done = f'{achieved:.2f} m of the requested {request.said}'
        if status == 'completed':
            verb = f'Turned {request.direction}' if request.turn else 'Moved forward'
            text = f'{verb} {done}, measured by phone tracking.'
        else:
            why = STOPPED_TEXT.get(reason, reason.replace('_', ' '))
            text = (f'Stopped {done}: {why}.' if achieved is None else
                    f'Stopped after {done}: {why}.')
            if request.turn and achieved is not None and achieved < 0:
                text = f'Stopped after turning {abs(achieved):.0f} degrees the wrong way: {why}.'
        self.result.update(status=status, reason=reason, text=text)


class Speak(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    move_id: str = Field(min_length=1, max_length=64)


def register_move_routes(app, runner_of, speak=None):
    """`GET /nav/move`: the latest move. `POST /nav/move/speak`: its final result, spoken once.

    The spoken text is the backend's own measured result, never model prose, and only exists
    once the move has ended, so speech can never claim a completion that did not happen.
    """
    from fastapi import HTTPException

    spoken = set()

    @app.get('/nav/move')
    async def move_status():
        result = runner_of().result
        return dict(version=1, move=None if result is None else dict(result))

    @app.post('/nav/move/speak')
    async def move_speak(body: Speak):
        if speak is None:
            raise HTTPException(503, 'Voice Q&A unavailable')
        result = runner_of().result
        if result is None or result['move_id'] != body.move_id:
            raise HTTPException(404, 'Unknown move')
        if result['status'] == 'running':
            raise HTTPException(409, 'move_running')
        if body.move_id in spoken:
            raise HTTPException(409, 'Already spoken')
        spoken.add(body.move_id)
        return dict(version=1, move_id=body.move_id, text=result['text'], speech=await speak(result['text']))
