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

import numpy as np

from backend.navigation import (FollowerConfig, Grid, PlannerConfig, PlanResult, PurePursuit,
                                corridor_alignment, nearest_frontier,
                                path_blocked, path_message, plan_path, preferred_explore_frontier)
from backend.occupancy import OCCUPIED, OccupancySnapshot

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
    replan_s: float = 4.  # keep a chosen side around an obstacle while the path is clear
    start_recovery_margin_m: float = 0.  # prototype only: escape an overlap with extra clearance
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
                     unknown_traversable=snapshot.unknown_traversable, footprint_clearance=True,
                     snap_radius_m=0.,
                     start_snap_radius_m=.6 if settings.start_recovery_margin_m > 0 else 0.)
    return grid, config


def obstacle_clearance_m(snapshot, x, z):
    """Distance from a camera-floor position to the nearest observed occupied cell."""
    if snapshot.cells is None or snapshot.origin is None:
        return 0.
    radius = snapshot.inflation_m + snapshot.cell_m
    row, col = snapshot._index(x, z)
    reach = math.ceil(radius / snapshot.cell_m) + 1
    r0, r1 = max(0, row - reach), min(snapshot.cells.shape[0], row + reach + 1)
    c0, c1 = max(0, col - reach), min(snapshot.cells.shape[1], col + reach + 1)
    rows, cols = np.where(snapshot.cells[r0:r1, c0:c1] == OCCUPIED)
    if not len(rows):
        return math.inf
    left = snapshot.origin[0] + (cols + c0) * snapshot.cell_m
    top = snapshot.origin[1] + (rows + r0) * snapshot.cell_m
    dx = np.maximum(np.maximum(left - x, x - left - snapshot.cell_m), 0.)
    dz = np.maximum(np.maximum(top - z, z - top - snapshot.cell_m), 0.)
    return float(np.min(np.hypot(dx, dz)))


def recoverable_start(snapshot, x, z, extra_margin_m):
    """Only the explicit prototype may drive away from a wall inside its extra margin."""
    return bool(extra_margin_m > 0 and snapshot.ready and snapshot.unknown_traversable
                and snapshot.cell(x, z) != OCCUPIED and snapshot.inflation_m is not None
                and obstacle_clearance_m(snapshot, x, z) >= snapshot.inflation_m - extra_margin_m)


def recovery_step_allowed(snapshot, x, z, next_x, next_z, extra_margin_m):
    if snapshot.traversable(next_x, next_z):
        return True
    if snapshot.traversable(x, z) or not recoverable_start(snapshot, x, z, extra_margin_m):
        return False
    before = obstacle_clearance_m(snapshot, x, z)
    after = obstacle_clearance_m(snapshot, next_x, next_z)
    return (snapshot.cell(next_x, next_z) != OCCUPIED and
            after >= snapshot.inflation_m - extra_margin_m and after >= before)


def straight_runway_m(path, segment, x, z, yaw):
    """Length of the current route aligned with the rover, before its next bend."""
    fx, fz = math.sin(yaw), math.cos(yaw)
    farthest = 0.
    for px, pz in path[max(0, segment):]:
        dx, dz = px - x, pz - z
        forward = dx * fx + dz * fz
        lateral = abs(dx * fz - dz * fx)
        if forward < farthest - .05 or lateral > .15:
            break
        farthest = max(farthest, forward)
    return farthest


class Navigator:
    def __init__(self, settings: NavSettings, *, pose: Callable[[], RoverPose | None],
                 occupancy: Callable[[], OccupancySnapshot | None], submit: Callable[[int, str, float, float], bool],
                 stop: Callable[[str], None], publish: Callable[[dict], None],
                 armed_mode: Callable[[], str | None], pause_reason: Callable[[], str | None] | None = None):
        """``armed_mode()`` is the current mode while armed, else None; a run whose mode
        it no longer matches stops at its next tick, even if ``halt()`` was missed."""
        self.settings = settings
        self._pose, self._occupancy, self._submit = pose, occupancy, submit
        self._stop, self._publish, self._armed_mode = stop, publish, armed_mode
        self._pause_reason = pause_reason or (lambda: None)
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

    def _plan(self, occupancy, start, goal, explore, yaw=0., explore_yaw=None):
        """Read the authoritative snapshot in this worker, then plan only known-clear floor."""
        def straight_ahead(points):
            fx, fz = math.sin(yaw), math.cos(yaw)
            farthest = 0.
            for px, pz in points[1:]:
                dx, dz = px - start[0], pz - start[1]
                forward = dx * fx + dz * fz
                lateral = abs(dx * fz - dz * fx)
                if forward < farthest - .05 or lateral > .12:
                    return False
                farthest = max(farthest, forward)
                if farthest >= 1.:
                    return True
            return False

        snapshot = occupancy()
        problem = map_problem(snapshot, self.settings.map_max_age_s)
        if problem:
            return 'plan', snapshot, goal, PlanResult([], problem)
        grid, config = planning_grid(snapshot, self.settings)
        if not (snapshot.traversable(*start) or
                recoverable_start(snapshot, *start, self.settings.start_recovery_margin_m)):
            return 'plan', snapshot, goal, PlanResult([], 'start_blocked')
        if explore:
            heading = yaw if explore_yaw is None else explore_yaw
            if (goal is None or grid.world_to_cell(*goal) is None
                    or not snapshot.traversable(*goal)):
                goal = preferred_explore_frontier(grid, start, heading, config,
                                                  allow_unknown=snapshot.unknown_traversable)
                if goal is None:
                    goal = nearest_frontier(grid, start, config, allow_unknown=snapshot.unknown_traversable)
                if goal is None:
                    return 'explore_complete', snapshot
            else:
                # Keep one cruise down the hallway as the camera reveals more
                # floor. A goal that was yesterday's frontier should advance
                # before the rover reaches it and brakes for another search.
                farther = preferred_explore_frontier(grid, start, heading, config,
                                                    allow_unknown=snapshot.unknown_traversable)
                if farther is not None:
                    fx, fz = math.sin(heading), math.cos(heading)
                    dx, dz = farther[0] - goal[0], farther[1] - goal[1]
                    if dx * fx + dz * fz >= .75 and abs(dx * fz - dz * fx) <= .35:
                        goal = farther
        result = plan_path(grid, start, goal, config)
        if explore and result.ok:
            corridor = corridor_alignment(grid, start, goal, yaw, config)
            centered = corridor is None
            if corridor is not None:
                via, width, offset = corridor
                centered = width >= 1.2 and abs(offset) < .12
                if abs(offset) >= .12:
                    first = plan_path(grid, start, via, config)
                    second = plan_path(grid, via, goal, config)
                    if first.ok and second.ok:
                        result = PlanResult(first.points[:-1] + second.points, None,
                                            first.expansions + second.expansions)
            result = replace(result, fast_corridor=centered and straight_ahead(result.points))
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
        fast_corridor = False
        explore_heading = None
        leg_origin = None
        try:
            while True:
                now = time.monotonic()
                if self._armed_mode() != RUN_MODES[kind]:
                    return self._finish('disarmed')
                if explore and (reason := self._pause_reason()) is not None:
                    if follower is not None or snapshot is not None or job is not None or progress is not None:
                        if job is not None:
                            job.cancel()
                            job = None
                        snapshot = follower = progress = None
                        fast_corridor = False
                        goal, last_plan = None, -math.inf
                        self._show([])
                        if not self._submit(generation, RUN_MODES[kind], 0., 0.):
                            return self._finish('command_stale')
                    await asyncio.sleep(period)
                    continue
                pose = self._pose()
                if pose is None or pose.age_s > s.pose_max_age_s:
                    return self._finish('pose_stale')
                if pose.tracking != 'normal':
                    return self._finish('tracking_lost')
                x, z, yaw = pose.x, pose.z, pose.yaw_rad
                if explore and explore_heading is None:
                    explore_heading = yaw

                if job is not None and job.done():
                    try:
                        outcome = job.result()
                    except Exception:
                        logger.exception('navigation planning failed')
                        return self._finish('nav_error')
                    job = None
                    outcome_kind, snapshot = outcome[0], outcome[1]
                    if outcome_kind == 'explore_complete':
                        # A mapped area can have no frontier until more camera
                        # evidence arrives. Stay in Explore at zero and check again.
                        goal, follower, progress, last_plan = None, None, None, now
                        fast_corridor = False
                        self._show([])
                        if not self._submit(generation, RUN_MODES[kind], 0., 0.):
                            return self._finish('command_stale')
                        await asyncio.sleep(period)
                        continue
                    if outcome_kind == 'check' and outcome[2]:
                        if explore and outcome[2] == 'path_blocked':
                            follower, progress, last_plan = None, None, -math.inf
                            fast_corridor = False
                            self._show([])
                            if not self._submit(generation, RUN_MODES[kind], 0., 0.):
                                return self._finish('command_stale')
                            await asyncio.sleep(period)
                            continue
                        return self._finish(outcome[2])
                    if outcome_kind == 'plan':
                        _, _, goal, result = outcome
                        if not result.ok:
                            if explore and result.reason in {'no_path', 'start_blocked', 'search_limit'}:
                                follower, progress, last_plan = None, None, now
                                fast_corridor = False
                                self._show([])
                                if not self._submit(generation, RUN_MODES[kind], 0., 0.):
                                    return self._finish('command_stale')
                                await asyncio.sleep(period)
                                continue
                            return self._finish(PLAN_STOP_REASONS.get(result.reason, result.reason))
                        self.goal = goal
                        if explore and leg_origin is None:
                            leg_origin = (x, z)
                        fast_corridor = result.fast_corridor
                        follower = (follower.replaced(result.points) if follower is not None
                                    else PurePursuit(result.points, s.follower))
                        self._show(result.points)

                if snapshot is not None:
                    problem = map_problem(snapshot, s.map_max_age_s)
                    if problem:
                        return self._finish(problem)
                if job is None:
                    occupancy = self._occupancy
                    goal_near = (explore and goal is not None and now - last_plan >= .5 and
                                 math.hypot(goal[0] - x, goal[1] - z) < 1.)
                    if snapshot is None or now - last_plan >= s.replan_s or goal_near:
                        last_plan = now
                        job = asyncio.ensure_future(asyncio.to_thread(
                            self._plan, occupancy, (x, z), goal, explore, yaw, explore_heading))
                    elif follower is not None and now - last_check >= s.blocked_check_s:
                        last_check = now
                        job = asyncio.ensure_future(asyncio.to_thread(
                            self._check, occupancy, list(follower.path), follower.segment, snapshot.revision))

                v = w = 0.
                if follower is not None and snapshot is not None:
                    if not (snapshot.traversable(x, z) or
                            recoverable_start(snapshot, x, z, s.start_recovery_margin_m)):
                        if explore:
                            follower, progress, last_plan = None, None, now
                            fast_corridor = False
                            self._show([])
                            if not self._submit(generation, RUN_MODES[kind], 0., 0.):
                                return self._finish('command_stale')
                            await asyncio.sleep(period)
                            continue
                        return self._finish('path_blocked')
                    command = follower.step(x, z, yaw)
                    if command.arrived:
                        if not explore:
                            return self._finish('arrived')
                        # Carry the same hallway bearing through short evasive
                        # turns. A completed long side leg becomes the bearing
                        # for the next hallway or branch.
                        if leg_origin is not None and math.hypot(x - leg_origin[0], z - leg_origin[1]) >= 1.2:
                            explore_heading = math.atan2(x - leg_origin[0], z - leg_origin[1])
                        leg_origin = (x, z)
                        # A replan/check belongs to the frontier just reached. Its
                        # late result must not restore that goal on the next tick.
                        if job is not None:
                            job.cancel()
                            job = None
                        follower, goal, last_plan = None, None, -math.inf  # next frontier
                        fast_corridor = False
                    else:
                        v, w = command.v_mps, command.yaw_rate_rps
                        if (explore and v >= .12 and w == 0. and
                                (fast_corridor or straight_runway_m(
                                    follower.path, follower.segment, x, z, yaw) >= .8)):
                            v = .2  # regain cruise on clear straight legs after an obstacle
                        # Reject a pursuit arc cutting a corner of the footprint-clear path.
                        heading = yaw + w * period / 2
                        if not recovery_step_allowed(
                                snapshot, x, z, x + v * math.sin(heading) * period,
                                z + v * math.cos(heading) * period, s.start_recovery_margin_m):
                            if explore:
                                follower, progress, last_plan = None, None, -math.inf
                                fast_corridor = False
                                self._show([])
                                if not self._submit(generation, RUN_MODES[kind], 0., 0.):
                                    return self._finish('command_stale')
                                await asyncio.sleep(period)
                                continue
                            return self._finish('path_blocked')

                if v or w:
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
