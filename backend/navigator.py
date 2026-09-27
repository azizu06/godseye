"""Async goal and explore runner over the live occupancy grid.

One run at a time. A run ticks at ``rate_hz`` on the event loop: it reads the latest
pose, steps a pure-pursuit follower and hands the command to ``submit`` (the app's
``Motion.submit``) under the arm generation the run started in; only the motion pump
ever talks to the car. A refused command means that generation ended, so the run
stops. Grid snapshots, blocked-path checks and A* run in worker threads, and their
results are picked up on a later tick, so the loop never blocks on planning.

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
from dataclasses import dataclass, field, replace
from typing import Callable


from backend.navigation import (FollowerConfig, Grid, PlannerConfig, PlanResult, PurePursuit, is_frontier,
                                nearest_frontier, path_blocked, path_message, plan_path)
from backend.occupancy import OccupancySnapshot

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
    rate_hz: float = 10.  # control ticks; each one submits a command
    replan_s: float = 1.  # full replan period
    blocked_check_s: float = .25  # at most this often, a grid change triggers a path_blocked check
    map_max_age_s: float = 1.  # accepted sensing, independent of poses and /live publication
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


def pose_from_transform(transform, camera_yaw_rad: float = 0.) -> tuple[float, float, float]:
    """Camera floor position and rover heading corrected by the measured mount yaw.

    Position stays in the v1 camera frame; map inflation already covers the
    chassis about that point. The live display's camera yaw remains unchanged.
    """
    yaw = math.atan2(-transform[8], -transform[10]) - camera_yaw_rad
    return transform[12], transform[14], math.atan2(math.sin(yaw), math.cos(yaw))


def map_problem(snapshot: OccupancySnapshot | None, max_age_s: float) -> str | None:
    """No footprint guesses, unmapped fallback or publication-based freshness."""
    if snapshot is None:
        return 'map_unknown'
    if snapshot.blockers:
        return snapshot.blockers[0]
    if snapshot.accepted_at is None or time.monotonic() - snapshot.accepted_at > max_age_s:
        return 'sensing_stale'
    return None


def planning_grid(snapshot, settings):
    grid = Grid.from_array(snapshot.cells, origin=snapshot.origin, cell_m=snapshot.cell_m)
    config = replace(settings.planner, robot_radius_m=snapshot.inflation_m, margin_m=0.,
                     unknown_traversable=False, footprint_clearance=True,
                     snap_radius_m=0., start_snap_radius_m=0.)
    return grid, config


class Navigator:
    def __init__(self, settings: NavSettings, *, pose: Callable[[], RoverPose | None],
                 occupancy: Callable[[], OccupancySnapshot | None], submit: Callable[[int, str, float, float], bool],
                 stop: Callable[[str], None], publish: Callable[[dict], None],
                 armed_mode: Callable[[], str | None]):
        """``armed_mode()`` is the current mode while armed, else None; a run whose mode
        it no longer matches stops at its next tick, even if ``halt()`` was missed."""
        self.settings = settings
        self._pose, self._occupancy, self._submit = pose, occupancy, submit
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
        """Read the authoritative snapshot in this worker, then plan only known-clear floor."""
        snapshot = occupancy()
        problem = map_problem(snapshot, self.settings.map_max_age_s)
        if problem:
            return 'plan', snapshot, goal, PlanResult([], problem)
        grid, config = planning_grid(snapshot, self.settings)
        if not snapshot.traversable(*start):
            return 'plan', snapshot, goal, PlanResult([], 'start_blocked')
        if explore:
            if goal is None or not is_frontier(grid, goal, config):
                goal = nearest_frontier(grid, start, config)
                if goal is None:
                    return 'explore_complete', snapshot
        result = plan_path(grid, start, goal, config)
        return 'plan', snapshot, goal, result

    def _check(self, occupancy, points, segment, revision=None):
        snapshot = occupancy()
        problem = map_problem(snapshot, self.settings.map_max_age_s)
        if not problem and snapshot.revision != revision:
            grid, config = planning_grid(snapshot, self.settings)
            if path_blocked(grid, points, config, segment):
                problem = 'path_blocked'
        return 'check', snapshot, problem

    async def plan_once(self, goal) -> PlanResult | None:
        """Initial plan for /goal; the app captures its arm generation before this await."""
        pose = self.fresh_pose()
        if pose is None:
            return None
        if pose.tracking != 'normal':
            return PlanResult([], 'tracking_lost')
        return (await asyncio.to_thread(self._plan, self._occupancy, (pose.x, pose.z), goal, False))[3]

    # Run lifecycle -----------------------------------------------------------------

    def start_goal(self, goal, initial, generation: int) -> None:
        """``generation`` is the arm generation read before planning began."""
        self._begin('goal', tuple(goal), initial, generation)

    def start_explore(self, generation: int) -> None:
        self._begin('explore', None, None, generation)

    def _begin(self, kind, goal, initial, generation) -> None:
        self._cancel()
        self.kind, self.goal = kind, goal
        self._task = asyncio.create_task(self._run(kind, goal, initial, generation))

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

    async def _run(self, kind, goal, initial, generation):
        s = self.settings
        explore = kind == 'explore'
        period = 1. / s.rate_hz
        follower = None
        snapshot = None
        last_plan = last_check = -math.inf
        if initial is not None:
            follower = PurePursuit(initial.points, s.follower)
            last_plan = time.monotonic()
            self._show(initial.points)
        job = None
        progress = None  # (x, z, yaw, since) while motion is commanded
        cleared_position = None  # previous checked pose, used to check swept motion
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
                    outcome_kind, snapshot = outcome[0], outcome[1]
                    if outcome_kind == 'explore_complete':
                        return self._finish('explore_complete')
                    if outcome_kind == 'check' and outcome[2]:
                        return self._finish(outcome[2])
                    if outcome_kind == 'plan':
                        _, _, goal, result = outcome
                        if not result.ok:
                            return self._finish(PLAN_STOP_REASONS.get(result.reason, result.reason))
                        self.goal = goal
                        follower = (follower.replaced(result.points) if follower is not None
                                    else PurePursuit(result.points, s.follower))
                        self._show(result.points)

                if snapshot is not None:
                    problem = map_problem(snapshot, s.map_max_age_s)
                    if problem:
                        return self._finish(problem)
                if job is None:
                    occupancy = self._occupancy
                    if snapshot is None or now - last_plan >= s.replan_s:  # -inf forces an immediate plan
                        last_plan = now
                        job = asyncio.ensure_future(asyncio.to_thread(
                            self._plan, occupancy, (x, z), goal, explore))
                    elif follower is not None and now - last_check >= s.blocked_check_s:
                        last_check = now
                        job = asyncio.ensure_future(asyncio.to_thread(
                            self._check, occupancy, list(follower.path), follower.segment, snapshot.revision))

                v = w = 0.
                if follower is not None and snapshot is not None:
                    if not snapshot.traversable(x, z):
                        return self._finish('path_blocked')
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
                        # Reject a pursuit arc cutting a corner of the footprint-clear path.
                        heading = yaw + w * period / 2
                        if not snapshot.traversable(x + v * math.sin(heading) * period,
                                                    z + v * math.cos(heading) * period):
                            return self._finish('path_blocked')

                if v or w:
                    # Check the actual displacement since the last command as well
                    # as the next commanded envelope. A pose jump cannot skip an
                    # unseen strip. Every footprint, including a stationary turn,
                    # needs fresh actual floor observations throughout.
                    previous = cleared_position
                    if previous is None:
                        if not snapshot.fresh_clearance(x, z, now, s.map_max_age_s):
                            return self._finish('sensing_clearance_unknown')
                        previous = (x, z)
                    distance = math.hypot(x-previous[0], z-previous[1])
                    steps = max(1, math.ceil(distance / (snapshot.cell_m / 2)))
                    if steps > 200:
                        return self._finish('sensing_clearance_unknown')
                    for i in range(1, steps+1):
                        next_position = (previous[0] + (x-previous[0])*i/steps,
                                         previous[1] + (z-previous[1])*i/steps)
                        if not snapshot.fresh_clearance(*next_position, now, s.map_max_age_s):
                            return self._finish('sensing_clearance_unknown')
                    heading = yaw + w * period / 2
                    destination = (x + v*math.sin(heading)*period, z + v*math.cos(heading)*period)
                    if not snapshot.fresh_clearance(*destination, now, s.map_max_age_s):
                        return self._finish('sensing_clearance_unknown')
                    cleared_position = (x, z)
                    if progress is None or (math.hypot(x - progress[0], z - progress[1]) >= s.progress_m
                                            or abs(math.remainder(yaw - progress[2], math.tau)) >= s.progress_rad):
                        progress = (x, z, yaw, now)
                    elif now - progress[3] >= s.no_progress_s:
                        return self._finish('no_progress')
                else:
                    progress = None
                if not self._submit(generation, RUN_MODES[kind], v, w):
                    return self._finish('command_stale')  # stopped or re-armed since this run began
                await asyncio.sleep(period)
        except Exception:  # a follower or bookkeeping bug must still stop the rover
            logger.exception('navigation run failed')
            return self._finish('nav_error')
        finally:
            if job is not None:
                job.cancel()
