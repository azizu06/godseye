"""One desired rover command per arm generation, dispatched by a bounded pump.

Every stop starts a new generation, so a goal or held button from before a
stop, reset, loss, mode switch or shutdown can never be sent again, not even
after a later arm. Nothing here reaches hardware; the car adapter decides that.

The adapter receives transport-neutral envelopes (see backend/README.md,
Command envelopes). Each successful arm opens a drive session that the next
Stop ends, so a car can tell a rearm from a reconnected link.
"""
from dataclasses import dataclass
import logging
import math
import time
from typing import Literal, Protocol
from uuid import uuid4

logger = logging.getLogger(__name__)
MAX_SPEED_MPS = .2  # contract hard maximums
MAX_YAW_RATE_RPS = .5
MAX_LEASE_S = .25  # the phase-2 command validity


@dataclass(frozen=True)
class DriveCommand:
    """A velocity for the car, possibly zero. No wire encoding is chosen here.

    `session_id` is the arm's drive session, not the phone map's. `seq` rises
    strictly within a session, across its commands and Stops. `issued_at_ms` is
    the backend's monotonic clock when it accepted this velocity (or made this
    zero), comparable only with other envelopes from the same backend process;
    with `valid_for_ms` it is the backend's lease, rounded to the millisecond.
    """
    session_id: str
    seq: int
    v_mps: float
    yaw_rate_rps: float
    issued_at_ms: int
    valid_for_ms: int


@dataclass(frozen=True)
class DriveStop:
    """Ends `session_id` for good; only a later arm's new session may move again.

    `session_id` is None before the first arm. A Stop never expires.
    """
    session_id: str | None
    seq: int
    issued_at_ms: int


class CarAdapter(Protocol):
    """Implemented in `backend.drive`; see its module docstring for the contract."""

    def send(self, command: DriveCommand) -> None: ...
    def zero(self, envelope: DriveCommand | DriveStop) -> None: ...
    def health(self) -> Literal['ok', 'stale', 'down']: ...


@dataclass(frozen=True)
class MotionLimits:
    max_speed_mps: float = .15  # contract start range is 0.10-0.15 m/s
    max_yaw_rate_rps: float = .5
    lease_s: float = .25  # a command not renewed within this is zeroed (dashboard resends every 100 ms)

    def __post_init__(self):
        for value, ceiling in ((self.max_speed_mps, MAX_SPEED_MPS), (self.max_yaw_rate_rps, MAX_YAW_RATE_RPS),
                               (self.lease_s, MAX_LEASE_S)):
            if not 0 < value <= ceiling:  # also false for NaN
                raise ValueError(f'motion limit {value} outside (0, {ceiling}]')


@dataclass(frozen=True)
class Command:
    generation: int
    mode: str
    v_mps: float
    yaw_rate_rps: float
    issued_at: float
    expires_at: float


def clamp(value, limit):
    return max(-limit, min(limit, value))


def ms(seconds):
    return round(seconds * 1000)


class Motion:
    """`check(command)` names why the command must not be sent now, else None.

    `stop(reason)` is the app's latched stop; it must call `halt()`. `tick()`
    is the only place a nonzero command reaches the car, once per call. Not
    thread-safe: call every method from the event loop.
    """

    def __init__(self, car: CarAdapter, check, stop, clock=time.monotonic, limits=MotionLimits()):
        self.car, self.check, self.stop, self.clock, self.limits = car, check, stop, clock, limits
        self.generation = 0
        self.active = False  # accepting commands: armed and not stopped since
        self.desired = None
        self.session_id = None  # the drive session of the latest arm; a Stop ends it
        self.seq = 0
        self.unsent = None  # the zero whose hand-off failed; nothing else is sent until it is resolved

    def begin(self):
        """Open a fresh generation and drive session after a successful arm; None if the car refused the zero.

        Arming zeroes too, so re-arming over a held command cannot leave a car
        that latches its last command moving, and no session opens before the
        previous one's Stop was handed off.
        """
        self.halt()
        if self.unsent is not None:
            return None
        self.session_id, self.seq = uuid4().hex, 0
        self.active = True
        return self.generation

    def halt(self):
        """Invalidate every held command, then hand the car a Stop that ends the session."""
        self.generation += 1
        self.active = False
        self.desired = None
        self.zero()

    def zero(self):
        """True once the car accepted an explicit zero.

        While armed (lease expiry, refused input) it is a zero command that keeps
        the session, and a failure becomes a car_error stop. Otherwise it is a
        Stop, retried unchanged by every later tick until the car accepts it or a
        later halt replaces it.
        """
        self.seq += 1
        now = ms(self.clock())
        if self.active:
            return self.hand_off(DriveCommand(self.session_id, self.seq, 0., 0., now, ms(self.limits.lease_s)))
        return self.hand_off(DriveStop(self.session_id, self.seq, now))

    def hand_off(self, envelope):
        try:
            self.car.zero(envelope)
        except Exception:
            if self.unsent is None:
                logger.exception('car zero failed; retrying every tick')
            self.unsent = envelope
            return False
        self.unsent = None
        return True

    def submit(self, generation, mode, v_mps, yaw_rate_rps):
        """Replace the desired command; False when `generation` is no longer the armed one.

        A non-finite command raises ValueError after zeroing the one it replaces.
        """
        if not self.active or generation != self.generation:
            return False
        if not (math.isfinite(v_mps) and math.isfinite(yaw_rate_rps)):
            self.desired = None
            self.zero()
            raise ValueError('non-finite drive command')
        now = self.clock()
        self.desired = Command(generation, mode, clamp(v_mps, self.limits.max_speed_mps),
                               clamp(yaw_rate_rps, self.limits.max_yaw_rate_rps), now, now + self.limits.lease_s)
        return True

    def tick(self):
        if self.unsent is not None:
            if self.active:
                self.stop('car_error')  # halt() hands off a Stop instead
            else:
                self.hand_off(self.unsent)  # the same Stop, unchanged
            return
        command = self.desired
        if command is None:
            return
        if self.clock() >= command.expires_at or command.generation != self.generation:
            self.desired = None
            if not self.zero() and self.active:
                self.stop('car_error')
            return
        if (reason := self.check(command)) is not None:
            self.stop(reason)
            return
        self.seq += 1
        try:
            self.car.send(DriveCommand(self.session_id, self.seq, command.v_mps, command.yaw_rate_rps,
                                       ms(command.issued_at), ms(self.limits.lease_s)))
        except Exception:
            logger.exception('car send failed')
            self.stop('car_error')
