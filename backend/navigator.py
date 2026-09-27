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

from backend.exploration import ExplorationMemory
from backend.navigation import (FollowerConfig, Grid, PlannerConfig, PlanResult, PurePursuit,
                                corridor_alignment, nearest_frontier,
                                path_blocked, path_message, plan_path, preferred_explore_frontier)
from backend.occupancy import FREE, OCCUPIED, OccupancySnapshot
from backend.scan_pacing import CameraPose, ScanPacer

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
    adaptive_explore: bool = False  # explicit prototype only; measured rate range stays intact
    rate_hz: float = 10.  # control ticks; each one submits a command
    replan_s: float = 4.  # keep a chosen side around an obstacle while the path is clear
    start_recovery_margin_m: float = 0.  # prototype only: escape an overlap with extra clearance
    blocked_check_s: float = .20  # at most this often, a grid change triggers a path_blocked check
    map_max_age_s: float = 1.  # accepted sensing, independent of poses and /live publication
    pose_max_age_s: float = .25  # matches the health/watchdog freshness rule
    no_progress_s: float = 5.  # commanded motion without pose change for this long stops the run
    progress_m: float = .05
    progress_rad: float = .15
    planner: PlannerConfig = field(default_factory=lambda: PlannerConfig(clearance_weight=4.))
    follower: FollowerConfig = field(default_factory=FollowerConfig)


@dataclass(frozen=True)
class RoverPose:
    x: float
    z: float
    yaw_rad: float
    age_s: float
    tracking: str
    camera: CameraPose | None = None


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


def obstacle_clearance_m(snapshot, x, z, search_m=0.):
    """Distance from a camera-floor position to the nearest observed occupied cell."""
    if snapshot.cells is None or snapshot.origin is None:
        return 0.
    radius = max(search_m, snapshot.inflation_m + snapshot.cell_m)
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


def exploration_command(snapshot, x, z, yaw, v, w, now, max_age_s):
    """Reduce nominal speed near obstacles/unknown floor/aging sensing; preserve curvature.

    This is an additional speed preference, never a substitute for swept-path
    validation or a measured physical stopping-distance guarantee.
    """
    if v <= 0:
        return v, w
    clearance = obstacle_clearance_m(snapshot, x, z, search_m=1.)
    room = max(0., min(1., (clearance - snapshot.inflation_m) / .4))
    freshness = max(0., min(1., 1. - (now - snapshot.accepted_at) / max_age_s))
    quality = max(0., min(1., snapshot.sensing_confidence))
    cap = .1 + .1 * room * freshness * quality
    # Unknown support is accepted only under the existing profile policy;
    # fewer observed free cells cannot justify the highest nominal cruise power.
    if any(snapshot.cell(x + d * math.sin(yaw), z + d * math.cos(yaw)) != 1
           for d in (.2, .4, .8)):
        cap = min(cap, .12)
    scale = min(1., cap / v)
    return v * scale, w * scale


def recoverable_start(snapshot, x, z, extra_margin_m):
    """Only the explicit prototype may drive away from a wall inside its extra margin."""
    return bool(extra_margin_m > 0 and snapshot.ready and snapshot.unknown_traversable
                and snapshot.cell(x, z) != OCCUPIED and snapshot.inflation_m is not None
                and obstacle_clearance_m(snapshot, x, z) >= snapshot.inflation_m - extra_margin_m)


def start_clearance_diagnostics(snapshot, x, z, extra_margin_m):
    """Report the exact start guard without granting a new motion permission."""
    clearance = obstacle_clearance_m(snapshot, x, z)
    clear = snapshot.traversable(x, z)
    recoverable = recoverable_start(snapshot, x, z, extra_margin_m)
    return dict(can_start=clear or recoverable, traversable=clear, recoverable=recoverable,
                camera_cell=snapshot.cell(x, z),
                nearest_occupied_m=clearance if math.isfinite(clearance) else None,
                inflation_m=snapshot.inflation_m,
                footprint_bound_m=max(0., snapshot.inflation_m-extra_margin_m),
                reason=None if clear or recoverable else 'start_blocked')


def recovery_step_allowed(snapshot, x, z, next_x, next_z, extra_margin_m):
    if not all(math.isfinite(v) for v in (x, z, next_x, next_z)):
        return False
    if snapshot.traversable(x, z):
        if not snapshot.traversable(next_x, next_z):
            return False
    elif not recoverable_start(snapshot, x, z, extra_margin_m):
        return False
    if snapshot.cell(next_x, next_z) == OCCUPIED:
        return False
    radius, cell = snapshot.inflation_m, snapshot.cell_m
    r0, c0 = snapshot._index(min(x, next_x) - radius, min(z, next_z) - radius)
    r1, c1 = snapshot._index(max(x, next_x) + radius, max(z, next_z) + radius)
    height, width = snapshot.cells.shape
    r0, c0 = min(height, max(0, r0)), min(width, max(0, c0))
    r1, c1 = min(height, max(0, r1 + 1)), min(width, max(0, c1 + 1))
    window = snapshot.cells[r0:r1, c0:c1]
    rows, cols = np.where(window == OCCUPIED if snapshot.unknown_traversable else window != FREE)
    left = snapshot.origin[0] + (cols + c0) * cell
    top = snapshot.origin[1] + (rows + r0) * cell
    def distances(px, pz):
        dx = np.maximum(np.maximum(left - px, px - left - cell), 0.)
        dz = np.maximum(np.maximum(top - pz, pz - top - cell), 0.)
        return np.hypot(dx, dz)
    before, after = distances(x, z), distances(next_x, next_z)
    dx, dz = next_x - x, next_z - z
    length2 = dx * dx + dz * dz
    closest = np.minimum(before, after)
    if length2:
        # Segment-to-box distance: endpoints, box corners projected onto the
        # segment, and intersections. Endpoints alone can cut across a corner.
        cx = np.stack((left, left + cell, left, left + cell), axis=1)
        cz = np.stack((top, top, top + cell, top + cell), axis=1)
        t = np.clip(((cx - x) * dx + (cz - z) * dz) / length2, 0., 1.)
        corners = np.hypot(cx - x - t * dx, cz - z - t * dz).min(axis=1)
        closest = np.minimum(closest, corners)
        enter, leave = np.zeros(len(left)), np.ones(len(left))
        interior_line = np.ones(len(left), dtype=bool)
        for origin, delta, low in ((x, dx, left), (z, dz, top)):
            if delta:
                a, b = (low - origin) / delta, (low + cell - origin) / delta
                enter, leave = np.maximum(enter, np.minimum(a, b)), np.minimum(leave, np.maximum(a, b))
            else:
                enter = np.where((origin >= low) & (origin <= low + cell), enter, np.inf)
                interior_line &= (origin > low) & (origin < low + cell)
        # Zero-clearance starts still cannot pass through occupied interiors.
        # An outward departure may touch a boundary only at the initial point.
        if np.any(interior_line & (enter < leave)):
            return False
        closest = np.where(enter <= leave, 0., closest)
    # Never deepen an existing overlap or enter another obstacle's clearance
    # area. Checking only the nearest obstacle misses a second one ahead.
    return bool(np.all(closest + 1e-9 >= np.minimum(before, radius)))


def pursuit_step_allowed(snapshot, x, z, yaw, v, w, period, distance, extra_margin_m):
    """Check the next tick and the nominal pursuit arc at half-cell spacing."""
    heading = yaw + w * period / 2
    if not recovery_step_allowed(snapshot, x, z, x + v * math.sin(heading) * period,
                                 z + v * math.cos(heading) * period, extra_margin_m):
        return False
    if v == 0:
        return True
    steps = max(1, math.ceil(distance / (snapshot.cell_m / 2)))
    dt = distance / (v * steps)
    for _ in range(steps):
        heading = yaw + w * dt / 2
        nx, nz = x + v * math.sin(heading) * dt, z + v * math.cos(heading) * dt
        if not recovery_step_allowed(snapshot, x, z, nx, nz, extra_margin_m):
            return False
        x, z, yaw = nx, nz, yaw + w * dt
    return True


def pursuit_step_allowed(snapshot, x, z, yaw, v, w, period, distance, extra_margin_m):
    """Check the immediate step and a sampled nominal arc up to the pursuit target.

    This uses requested kinematics, not a physical PWM/stopping-distance model.
    Each sample uses the same strict clearance/overlap-exit rule as the next tick.
    """
    heading = yaw + w * period / 2
    if not recovery_step_allowed(snapshot, x, z, x + v * math.sin(heading) * period,
                                 z + v * math.cos(heading) * period, extra_margin_m):
        return False
    if v == 0:
        return True
    steps = max(1, math.ceil(distance / (snapshot.cell_m / 2)))
    dt = distance / (v * steps)
    for _ in range(steps):
        heading = yaw + w * dt / 2
        nx, nz = x + v * math.sin(heading) * dt, z + v * math.cos(heading) * dt
        if not recovery_step_allowed(snapshot, x, z, nx, nz, extra_margin_m):
            return False
        x, z, yaw = nx, nz, yaw + w * dt
    return True


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
                 armed_mode: Callable[[], str | None], pause_reason: Callable[[], str | None] | None = None,
                 scan_observation: Callable | None = None):
        """``armed_mode()`` is the current mode while armed, else None; a run whose mode
        it no longer matches stops at its next tick, even if ``halt()`` was missed."""
        self.settings = settings
        self._pose, self._occupancy, self._submit = pose, occupancy, submit
        self._stop, self._publish, self._armed_mode = stop, publish, armed_mode
        self._pause_reason = pause_reason or (lambda: None)
        self._scan_observation = scan_observation
        self.scan_status = None
        self._task: asyncio.Task | None = None
        self._shown = None  # points of the last published path, to publish only changes
        self.kind: str | None = None  # 'goal' or 'explore' while a run is active
        self.goal: tuple[float, float] | None = None
        self.exploration = ExplorationMemory()
        self.waiting_reason: str | None = None

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

    def _plan(self, occupancy, start, goal, explore, yaw=0., explore_yaw=None, excluded=()):
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
        if not start_clearance_diagnostics(snapshot, *start, self.settings.start_recovery_margin_m)['can_start']:
            return 'plan', snapshot, goal, PlanResult([], 'start_blocked')
        heading = yaw if explore_yaw is None else explore_yaw

        target_mask = self.exploration.targets(snapshot, grid) if explore else None

        def next_frontier():
            preferred = preferred_explore_frontier(grid, start, heading, config,
                                                    allow_unknown=snapshot.unknown_traversable,
                                                    excluded=excluded, target_mask=target_mask)
            return (preferred if preferred is not None else
                    nearest_frontier(grid, start, config,
                                     allow_unknown=snapshot.unknown_traversable,
                                     excluded=excluded, target_mask=target_mask))

        if explore:
            if (goal is None or grid.world_to_cell(*goal) is None
                    or not snapshot.traversable(*goal)):
                goal = next_frontier()
                if goal is None:
                    return 'explore_complete', snapshot
            else:
                # Keep one cruise down the hallway as the camera reveals more
                # floor. A goal that was yesterday's frontier should advance
                # before the rover reaches it and brakes for another search.
                farther = preferred_explore_frontier(grid, start, heading, config,
                                                    allow_unknown=snapshot.unknown_traversable, excluded=excluded, target_mask=target_mask)
                if farther is not None:
                    fx, fz = math.sin(heading), math.cos(heading)
                    dx, dz = farther[0] - goal[0], farther[1] - goal[1]
                    if dx * fx + dz * fz >= .75 and abs(dx * fz - dz * fx) <= .35:
                        goal = farther
        result = plan_path(grid, start, goal, config)
        if explore and result.reason == 'no_path':
            self.exploration.reject(snapshot.session, goal)
            # A new wall can disconnect an otherwise free implicit frontier.
            # First try its detour above; only proven unreachability permits a
            # different reachable frontier, with one bounded retry on this map.
            replacement = next_frontier()
            if replacement is None:
                return 'explore_complete', snapshot
            if replacement != goal:
                goal = replacement
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
        self.waiting_reason = None
        self.scan_status = None
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
        pacer = ScanPacer() if explore and self._scan_observation is not None else None
        self.scan_status = pacer.status if pacer is not None else None
        period = 1. / s.rate_hz
        follower = None
        snapshot = None
        last_plan = last_check = -math.inf
        if initial is not None:
            follower = PurePursuit(initial.points, s.follower)
            last_plan = time.monotonic()
            self._show(initial.points)
        job = check_job = None
        checking_follower = None
        checked_revision = None
        visited = []
        progress = None  # (x, z, yaw, since) while motion is commanded
        fast_corridor = False
        explore_heading = None
        leg_origin = None
        rejected = None  # session/revision/path/pose for a pursuit step that cannot move
        try:
            while True:
                now = time.monotonic()
                if self._armed_mode() != RUN_MODES[kind]:
                    return self._finish('disarmed')
                if explore and (reason := self._pause_reason()) is not None:
                    self.waiting_reason = reason
                    if pacer is not None and pacer.active:
                        pacer.finish('interrupted')
                    if follower is not None or snapshot is not None or job is not None or progress is not None:
                        if job is not None:
                            job.cancel()
                            job = None
                        if check_job is not None:
                            check_job.cancel()
                            check_job = None
                        snapshot = follower = progress = None
                        checked_revision = None
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

                # Reading current sensing never waits behind a route search. A
                # route job owns geometry; the independent check owns freshness.
                outcomes = []
                if check_job is not None and check_job.done():
                    try:
                        checked = check_job.result()
                    except Exception:
                        logger.exception('navigation map check failed')
                        return self._finish('nav_error')
                    check_job = None
                    if checking_follower is not follower and checked[2] == 'path_blocked':
                        checked = ('check', checked[1], None)
                    elif checking_follower is follower and checked[1] is not None:
                        checked_revision = checked[1].revision
                    outcomes.append(checked)
                if job is not None and job.done():
                    try:
                        outcomes.append(job.result())
                    except Exception:
                        logger.exception('navigation planning failed')
                        return self._finish('nav_error')
                    job = None
                for outcome in outcomes:
                    outcome_kind, observed = outcome[0], outcome[1]
                    # A failed search (or exhausted frontier set) describes the
                    # map it searched. Do not disarm or blacklist a goal after
                    # newer geometry has already cleared that obstruction.
                    failed_plan = (outcome_kind == 'explore_complete' or
                                   (outcome_kind == 'plan' and not outcome[3].ok))
                    if (outcome_kind == 'plan' and not outcome[3].ok
                            and outcome[3].reason in {'sensing_stale', 'map_unknown'}
                            and map_problem(snapshot, s.map_max_age_s) is None):
                        last_plan = -math.inf
                        continue
                    if (failed_plan and snapshot is not None and observed is not None
                            and (snapshot.accepted_at or 0.) > (observed.accepted_at or 0.)
                            and (snapshot.session != observed.session
                                 or snapshot.origin != observed.origin
                                 or snapshot.inflation_m != observed.inflation_m
                                 or snapshot.unknown_traversable != observed.unknown_traversable
                                 or snapshot.blockers != observed.blockers
                                 or not np.array_equal(snapshot.cells, observed.cells))):
                        last_plan = -math.inf
                        continue
                    if (snapshot is None or observed is None or observed.session != snapshot.session
                            or (observed.accepted_at or 0.) >= (snapshot.accepted_at or 0.)):
                        snapshot = observed
                    if outcome_kind == 'explore_complete':
                        goal, follower, progress, last_plan = None, None, None, now
                        fast_corridor = False
                        self.waiting_reason = 'explore_complete'
                        self._show([])
                        continue
                    if outcome_kind == 'check' and outcome[2]:
                        if outcome[2] == 'path_blocked':
                            # Keep travelling on the still-clear prefix while a
                            # replacement route is computed. The command rollout
                            # below prevents continuing into the obstruction.
                            last_plan = -math.inf
                        else:
                            return self._finish(outcome[2])
                    if outcome_kind == 'plan':
                        _, _, next_goal, result = outcome
                        if not result.ok:
                            if explore and result.reason in {'no_path', 'start_blocked', 'search_limit'}:
                                if next_goal is not None and result.reason == 'no_path':
                                    visited.append(next_goal)
                                    visited = visited[-64:]
                                goal = follower = progress = None
                                last_plan = now - s.replan_s + .5
                                fast_corridor = False
                                self.waiting_reason = result.reason
                                self._show([])
                                continue
                            return self._finish(PLAN_STOP_REASONS.get(result.reason, result.reason))
                        goal = self.goal = next_goal
                        if explore and leg_origin is None:
                            leg_origin = (x, z)
                        fast_corridor = result.fast_corridor
                        follower = (follower.replaced(result.points) if follower is not None
                                    else PurePursuit(result.points, s.follower))
                        last_plan = now
                        last_check = -math.inf
                        checked_revision = None  # this new route has not been checked on the newest map
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
                            self._plan, occupancy, (x, z), goal, explore, yaw, explore_heading, tuple(visited)))
                if check_job is None and now - last_check >= s.blocked_check_s:
                    last_check = now
                    checking_follower = follower
                    remaining = ([[x, z]] + list(follower.path[follower.segment + 1:])
                                 if follower is not None else [])
                    check_job = asyncio.ensure_future(asyncio.to_thread(
                        self._check, self._occupancy, remaining, 0,
                        checked_revision))

                if explore and snapshot is not None:
                    loop = self.exploration.observe(snapshot.session, x, z, yaw)
                    if loop and goal is not None:
                        self.exploration.reject(snapshot.session, goal)
                        if job is not None:
                            job.cancel()
                            job = None
                        follower, goal, progress, last_plan = None, None, None, -math.inf
                        self.waiting_reason = 'explore_loop_replan'
                        self._show([])

                v = w = 0.
                command_blocked = False
                if follower is not None and snapshot is not None:
                    if not (snapshot.traversable(x, z) or
                            recoverable_start(snapshot, x, z, s.start_recovery_margin_m)):
                        if explore:
                            follower, progress, last_plan = None, None, now
                            fast_corridor = False
                            self.waiting_reason = 'start_blocked'
                            if pacer is not None and pacer.active:
                                pacer.finish('interrupted')
                            self._show([])
                            if not self._submit(generation, RUN_MODES[kind], 0., 0.):
                                return self._finish('command_stale')
                            await asyncio.sleep(period)
                            continue
                        return self._finish('path_blocked')
                    if rejected is not None:
                        session, revision, points, rx, rz, ryaw = rejected
                        if (session == snapshot.session and revision == snapshot.revision
                                and points == follower.path
                                and math.hypot(x - rx, z - rz) < s.progress_m
                                and abs(math.remainder(yaw - ryaw, math.tau)) < s.progress_rad):
                            self.waiting_reason = 'no_feasible_step'
                            if pacer is not None and pacer.active:
                                pacer.finish('interrupted')
                            if not self._submit(generation, RUN_MODES[kind], 0., 0.):
                                return self._finish('command_stale')
                            await asyncio.sleep(period)
                            continue
                        rejected = None
                    self.waiting_reason = None
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
                        if goal is not None:
                            visited.append(goal)
                            visited = visited[-64:]
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
                        lookahead = follower.config.lookahead_m
                        target = command.target
                        minimum = max(snapshot.cell_m, follower.config.arrive_tolerance_m)

                        def allowed(v, w, target, lookahead):
                            distance = (min(lookahead, math.dist((x, z), target))
                                        if target is not None else lookahead)
                            return pursuit_step_allowed(snapshot, x, z, yaw, v, w, period,
                                                        distance, s.start_recovery_margin_m)

                        feasible = allowed(v, w, target, lookahead)
                        while not feasible and lookahead > minimum:
                            lookahead = max(minimum, lookahead / 2)
                            candidate = follower.step(x, z, yaw, lookahead_m=lookahead)
                            v, w, target = candidate.v_mps, candidate.yaw_rate_rps, candidate.target
                            feasible = allowed(v, w, target, lookahead)
                        if not feasible:
                            if explore:
                                rejected = (snapshot.session, snapshot.revision, list(follower.path), x, z, yaw)
                                progress = None  # zero wait is not commanded-motion stall
                                self.waiting_reason = 'no_feasible_step'
                                last_plan = now
                                v = w = 0.
                            else:
                                v = w = 0.
                                command_blocked = True
                                last_plan = -math.inf

                if explore and s.adaptive_explore and snapshot is not None:
                    v, w = exploration_command(snapshot, x, z, yaw, v, w, now, s.map_max_age_s)

                if pacer is not None:
                    feasible_forward = v > 0. and abs(w) < .1 and self.waiting_reason is None
                    if pacer.step(now, pose.camera, self._scan_observation(), feasible_forward):
                        v = w = 0.
                        self.waiting_reason = 'scan_' + pacer.status['phase']

                if v or w or command_blocked:
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
            if check_job is not None:
                check_job.cancel()
