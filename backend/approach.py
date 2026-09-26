"""Suggested walking approach to a localized person: visualization only.

Reuses the rover planner's grid A* with a human walker's clearance instead of the
car footprint, over observed-free cells only: unknown space is a wall (never
filled in), doors are not inferred, and the route ends at an observed-free
approach point beside the person, never on the person's own cells. Nothing here
sets a rover goal, publishes a `path`, or commands motion.
"""
import math

import numpy as np

from .navigation import FREE, OCCUPIED, Grid, PlannerConfig, plan_path

# Demo clearance assumptions, reported with every route.
WALKER_RADIUS_M = .25  # an upright responder needs a ~0.5 m wide passage
MARGIN_M = .05  # extra distance kept from every observed obstacle and unknown cell
PERSON_KEEP_OUT_M = .3  # the person's own footprint; the route never enters it
APPROACH_REACH_M = 1.5  # the approach point must lie within this of the person
START_SNAP_M = .3  # an operator click may land just inside a clearance band

ASSUMPTIONS = dict(walker_radius_m=WALKER_RADIUS_M, margin_m=MARGIN_M,
                   person_keep_out_m=PERSON_KEEP_OUT_M, approach_reach_m=APPROACH_REACH_M,
                   unknown='blocked', doors='not_inferred', verified=False)
CONFIG = PlannerConfig(robot_radius_m=WALKER_RADIUS_M, margin_m=MARGIN_M, unknown_traversable=False,
                       footprint_clearance=True, snap_radius_m=APPROACH_REACH_M,
                       start_snap_radius_m=START_SNAP_M, waypoint_spacing_m=.25)
REASONS = {  # plan_path failures, in operator terms
    'out_of_bounds': 'start_or_person_off_map',
    'start_blocked': 'start_not_observed_free',
    'goal_occupied': 'no_observed_free_approach',
    'goal_unknown': 'no_observed_free_approach',
    'no_path': 'no_observed_free_route',
    'search_limit': 'no_observed_free_route',
}


def _keep_out(grid: Grid, xz) -> Grid:
    """The grid with the person's own footprint marked occupied."""
    cells = np.array(grid.cells)
    rows = grid.origin[1] + (np.arange(grid.height) + .5) * grid.cell_m
    cols = grid.origin[0] + (np.arange(grid.width) + .5) * grid.cell_m
    near = (rows[:, None] - xz[1]) ** 2 + (cols[None, :] - xz[0]) ** 2 <= PERSON_KEEP_OUT_M ** 2
    cells[near] = OCCUPIED
    row, col = grid.cell_of(*xz)
    if 0 <= row < grid.height and 0 <= col < grid.width:
        cells[row, col] = OCCUPIED  # even a person narrower than one cell
    return Grid.from_array(cells, origin=grid.origin, cell_m=grid.cell_m)


def approach_route(grid: Grid | None, start_xz, person_xz) -> dict:
    """`{status: "ok", points, approach, length_m}` or `{status: "unavailable", reason}`.

    `points` run from the operator-selected start to the approach point in world
    (x, z). Every result carries `assumptions`; a route is never verified.
    """
    base = dict(assumptions=ASSUMPTIONS)
    if grid is None or not np.any(grid.cells == FREE):
        return dict(base, status='unavailable', reason='no_observed_map')
    if not all(math.isfinite(v) for v in (*start_xz, *person_xz)):
        return dict(base, status='unavailable', reason='invalid_point')
    if grid.world_to_cell(*person_xz) is None:
        return dict(base, status='unavailable', reason='start_or_person_off_map')
    result = plan_path(_keep_out(grid, person_xz), start_xz, person_xz, CONFIG)
    if not result.ok:
        return dict(base, status='unavailable', reason=REASONS.get(result.reason, 'no_observed_free_route'))
    points = [[round(x, 3), round(z, 3)] for x, z in result.points]
    length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
    return dict(base, status='ok', points=points, approach=points[-1], length_m=round(length, 2))

