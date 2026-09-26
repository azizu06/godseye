"""Async goal and explore runner over the live occupancy grid; drives only the logging stub.

One run at a time. A run ticks at ``rate_hz`` on the event loop: it reads the latest
pose, steps a pure-pursuit follower and calls ``drive``. Grid snapshots, blocked-path
checks and A* run in worker threads, and their results are picked up on a later tick,
so the loop never blocks on planning.

Every way a run ends goes through the app's ``stop(reason)``, which disarms, sends a
zero drive and logs a health event; the run then clears the dashboard path. Stops raised
elsewhere (operator, mode change, map reset, tracking loss, stale pose, phone loss,
shutdown) reach the run through ``halt()``, called from ``stop``.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from backend.navigation import (FollowerConfig, Grid, PlannerConfig, PlanResult, PurePursuit, is_frontier,
                                nearest_frontier, path_blocked, path_message, plan_path)
from backend.occupancy import CELL_M

logger = logging.getLogger(__name__)

# plan_path reasons -> the stop_reason reported in health.
PLAN_STOP_REASONS = {
    'goal_occupied': 'destination_blocked',
    'goal_unknown': 'destination_unknown',
    'no_path': 'no_path',
    'search_limit': 'search_limit',
    'out_of_bounds': 'out_of_bounds',
    'start_blocked': 'start_blocked',
}
RUN_MODES = {'goal': 'navigate', 'explore': 'explore'}  # run kind -> the mode it needs armed


@dataclass(frozen=True)
class NavSettings:
    rate_hz: float = 10.  # control ticks; each one calls drive()
    replan_s: float = 1.  # full replan period
    blocked_check_s: float = .25  # at most this often, a grid change triggers a path_blocked check
    pose_max_age_s: float = .25  # matches the health/watchdog freshness rule
    no_progress_s: float = 5.  # commanded motion without pose change for this long stops the run
    progress_m: float = .05
    progress_rad: float = .15
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    follower: FollowerConfig = field(default_factory=FollowerConfig)


@dataclass(frozen=True)
class RoverPose:
    x: float
    z: float
    yaw_rad: float
    age_s: float
    tracking: str


def pose_from_transform(transform) -> tuple[float, float, float]:
    """(x, z, yaw_rad) with the same yaw as the live pose message (camera forward, -Z)."""
    return transform[12], transform[14], math.atan2(-transform[8], -transform[10])


def grid_snapshot(occupancy) -> tuple[int | None, Grid | None]:
    """(revision, planning Grid or None) from an OccupancyGrid; blocking, run in a thread."""
    if occupancy is None:
        return None, None
    revision, origin, cells = occupancy.snapshot()
    if cells is None:
        return revision, None
    return revision, Grid.from_array(cells, origin=origin, cell_m=CELL_M)


def _unmapped(x: float, z: float) -> Grid:
    """A one-cell unknown grid at the rover; plan_path pads it to reach any goal."""
    return Grid.from_array(np.zeros((1, 1), dtype=np.uint8), origin=(x, z), cell_m=CELL_M)


class Navigator:
    def __init__(self, settings: NavSettings, *, pose: Callable[[], RoverPose | None],
                 occupancy: Callable[[], object], drive: Callable[[float, float], None],
                 stop: Callable[[str], None], publish: Callable[[dict], None],
                 armed_mode: Callable[[], str | None]):
        """``armed_mode()`` is the current mode while armed, else None; a run whose mode
        it no longer matches stops at its next tick, even if ``halt()`` was missed."""
        self.settings = settings
        self._pose, self._occupancy, self._drive = pose, occupancy, drive
        self._stop, self._publish, self._armed_mode = stop, publish, armed_mode
        self._task: asyncio.Task | None = None
        self._shown = None  # points of the last published path, to publish only changes
        self.kind: str | None = None  # 'goal' or 'explore' while a run is active
        self.goal: tuple[float, float] | None = None

    @property
    def active(self) -> bool:
        return self._task is not None

    @property
    def path(self) -> list:
        """The path the dashboard was last sent; empty when no run is following one."""
        return self._shown or []

    def fresh_pose(self) -> RoverPose | None:
        """The current pose if it is recent enough to plan from, else None."""
        pose = self._pose()
        if pose is None or pose.age_s > self.settings.pose_max_age_s:
            return None
        return pose

    # Planning runs in worker threads -------------------------------------------------

    def _plan(self, occupancy, start, goal, explore):
        """('plan', revision, goal, PlanResult), ('explore_complete', revision), or
        ('waiting', revision) while exploring before any floor has been mapped."""
        revision, grid = grid_snapshot(occupancy)
        if explore:
            if grid is None:
                return 'waiting', revision
            if goal is None or not is_frontier(grid, goal):
                goal = nearest_frontier(grid, start, self.settings.planner)
                if goal is None:
                    return 'explore_complete', revision
        result = plan_path(grid if grid is not None else _unmapped(*start), start, goal, self.settings.planner)
        return 'plan', revision, goal, result

    def _check(self, occupancy, points, segment):
        """('check', revision, blocked)."""
        revision, grid = grid_snapshot(occupancy)
        blocked = grid is not None and path_blocked(grid, points, self.settings.planner, segment)
        return 'check', revision, blocked

    async def plan_once(self, goal) -> PlanResult | None:
        """Initial plan for /goal from the current pose; None without a fresh pose."""
        pose = self.fresh_pose()
        if pose is None:
            return None
        return (await asyncio.to_thread(self._plan, self._occupancy(), (pose.x, pose.z), goal, False))[3]

    # Run lifecycle -----------------------------------------------------------------

    def start_goal(self, goal, initial) -> None:
        self._begin('goal', tuple(goal), initial)

    def start_explore(self) -> None:
        self._begin('explore', None, None)

    def _begin(self, kind, goal, initial) -> None:
        self._cancel()
        self.kind, self.goal = kind, goal
        self._task = asyncio.create_task(self._run(kind, goal, initial))

    def _cancel(self) -> asyncio.Task | None:
        task, self._task = self._task, None
        self.kind = self.goal = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        return task

    def _show(self, points) -> None:
        if points != self._shown:
            self._shown = points
            self._publish(path_message(points))

    def halt(self) -> None:
        """Stop following and clear the dashboard path; called from the app's stop()."""
        if self._cancel() is not None:
            self._show([])

    async def aclose(self) -> None:
        """Halt and wait for the run task to finish unwinding (app shutdown)."""
        task = self._cancel()
        if task is not None:
            self._show([])
            if task is not asyncio.current_task():
                with suppress(asyncio.CancelledError):
                    await task

    def _finish(self, reason: str) -> None:
        logger.info('navigation ended: %s', reason)
        self._stop(reason)  # disarms, zero drive, and calls halt()

    async def _run(self, kind, goal, initial):
        s = self.settings
        explore = kind == 'explore'
        period = 1. / s.rate_hz
        follower = None
        revision = None
        last_plan = last_check = -math.inf
        if initial is not None:
            follower = PurePursuit(initial.points, s.follower)
            last_plan = time.monotonic()
            self._show(initial.points)
        job = None
        progress = None  # (x, z, yaw, since) while motion is commanded
        try:
            while True:
                now = time.monotonic()
                if self._armed_mode() != RUN_MODES[kind]:
                    return self._finish('disarmed')
                pose = self._pose()
                if pose is None or pose.age_s > s.pose_max_age_s:
                    return self._finish('pose_stale')
                if pose.tracking != 'normal':
                    return self._finish('tracking_lost')
                x, z, yaw = pose.x, pose.z, pose.yaw_rad

                if job is not None and job.done():
                    try:
                        outcome = job.result()
                    except Exception:
                        logger.exception('navigation planning failed')
                        return self._finish('nav_error')
                    job = None
                    outcome_kind, revision = outcome[0], outcome[1]
                    if outcome_kind == 'explore_complete':
                        return self._finish('explore_complete')
                    if outcome_kind == 'check' and outcome[2]:
                        follower, last_plan = None, -math.inf  # halt now and replan at once
                    if outcome_kind == 'plan':
                        _, _, goal, result = outcome
                        if not result.ok:
                            return self._finish(PLAN_STOP_REASONS.get(result.reason, 'no_path'))
                        self.goal = goal
                        follower = (follower.replaced(result.points) if follower is not None
                                    else PurePursuit(result.points, s.follower))
                        self._show(result.points)

                if job is None:
                    occupancy = self._occupancy()
                    latest = getattr(occupancy, 'revision', None)
                    if now - last_plan >= s.replan_s:  # -inf forces an immediate plan
                        last_plan = now
                        job = asyncio.ensure_future(asyncio.to_thread(
                            self._plan, occupancy, (x, z), goal, explore))
                    elif follower is not None and latest != revision and now - last_check >= s.blocked_check_s:
                        last_check = now
                        job = asyncio.ensure_future(asyncio.to_thread(
                            self._check, occupancy, list(follower.path), follower.segment))

                v = w = 0.
                if follower is not None:
                    command = follower.step(x, z, yaw)
                    if command.arrived:
                        if not explore:
                            return self._finish('arrived')
                        # A replan/check belongs to the frontier just reached. Its
                        # late result must not restore that goal on the next tick.
                        if job is not None:
                            job.cancel()
                            job = None
                        follower, goal, last_plan = None, None, -math.inf  # next frontier
                    else:
                        v, w = command.v_mps, command.yaw_rate_rps

                if v or w:
                    if progress is None or (math.hypot(x - progress[0], z - progress[1]) >= s.progress_m
                                            or abs(math.remainder(yaw - progress[2], math.tau)) >= s.progress_rad):
                        progress = (x, z, yaw, now)
                    elif now - progress[3] >= s.no_progress_s:
                        return self._finish('no_progress')
                else:
                    progress = None
                self._drive(v, w)
                await asyncio.sleep(period)
        except Exception:  # a follower or bookkeeping bug must still stop the rover
            logger.exception('navigation run failed')
            return self._finish('nav_error')
        finally:
            if job is not None:
                job.cancel()
