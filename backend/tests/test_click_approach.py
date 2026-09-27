"""Click-to-approach planning: an occupied or unknown clicked target resolves to a stand-off
the unchanged planner accepts; the default /goal planning is untouched. Pure grids only."""
import math
import unittest

import numpy as np

from backend.navigation import (APPROACH_RADIUS_M, Grid, PlannerConfig, plan_approach, plan_path,
                                traversable_mask)

UNKNOWN, FREE, OCCUPIED = 0, 1, 2
# The live navigator's policy: unknown is a wall and the whole footprint must stay clear.
LIVE = PlannerConfig(unknown_traversable=False, footprint_clearance=True)


def room(fill=FREE, width=80, height=80, cell_m=.05):
    return np.full((height, width), fill, dtype=np.uint8)


def as_grid(cells, cell_m=.05):
    return Grid.from_array(cells, origin=(0., 0.), cell_m=cell_m)


def box(cells, x0, z0, x1, z1, value=OCCUPIED, cell_m=.05):
    """Mark the world rectangle [x0, x1) x [z0, z1)."""
    cells[int(round(z0 / cell_m)):int(round(z1 / cell_m)), int(round(x0 / cell_m)):int(round(x1 / cell_m))] = value


class ClickApproachTests(unittest.TestCase):
    def assert_planner_accepts(self, grid, start, result, config):
        self.assertTrue(result.ok, result.reason)
        goal = result.points[-1]
        cell = grid.world_to_cell(*goal)
        self.assertIsNotNone(cell)
        self.assertTrue(traversable_mask(grid, config)[cell], 'stand-off is inside inflation')
        # The stand-off is itself a goal the default planner accepts, unchanged.
        again = plan_path(grid, start, goal, config)
        self.assertTrue(again.ok, again.reason)
        self.assertEqual(again.points[-1], goal)

    def test_occupied_target_snaps_to_a_reachable_stand_off_on_the_rover_side(self):
        cells = room()
        box(cells, 1.8, 1.8, 2.2, 2.2)  # a 0.4 m box in the middle of the room
        g = as_grid(cells)
        start, target = (2., .5), (2., 2.)  # clicked the middle of the box from the south
        for config in (PlannerConfig(), LIVE):
            self.assertEqual(plan_path(g, start, target, config).reason, 'goal_occupied')
            result = plan_approach(g, start, target, config)
            self.assert_planner_accepts(g, start, result, config)
            gx, gz = result.points[-1]
            self.assertLess(gz, 1.8, 'stand-off must be on the rover side of the box')
            self.assertLessEqual(math.hypot(gx - target[0], gz - target[1]), APPROACH_RADIUS_M)
            self.assertLess(math.hypot(gx - target[0], gz - target[1]), .6, 'stand-off should hug the box')

    def test_wall_click_stays_on_the_rover_side_of_the_wall(self):
        cells = room()
        box(cells, 0., 2.5, 3.0, 2.6)  # a wall with a gap at the east end
        g = as_grid(cells)
        start, target = (1., 1.), (1., 2.55)
        result = plan_approach(g, start, target, LIVE)
        self.assert_planner_accepts(g, start, result, LIVE)
        self.assertLess(result.points[-1][1], 2.5)

    def test_unknown_target_snaps_to_known_free_floor(self):
        cells = room()
        box(cells, 0., 2.5, 4., 4., value=UNKNOWN)  # unscanned far half of the room
        g = as_grid(cells)
        start, target = (2., .5), (2., 2.8)
        self.assertEqual(plan_path(g, start, target, LIVE).reason, 'goal_unknown')
        result = plan_approach(g, start, target, LIVE)
        self.assert_planner_accepts(g, start, result, LIVE)
        self.assertLess(result.points[-1][1], 2.5)

    def test_no_reachable_stand_off_keeps_the_original_refusal(self):
        cells = room()
        box(cells, .4, .5, 3.8, 3.8)  # a solid block whose free floor is all beyond the radius
        g = as_grid(cells)
        start, target = (2., .2), (2.1, 2.2)
        for config in (PlannerConfig(), LIVE):
            direct = plan_path(g, start, target, config)
            self.assertEqual(plan_approach(g, start, target, config), direct)
        # Free floor exists near the target, but only behind a closed wall: still refused.
        cells = room()
        box(cells, 0., 2., 4., 2.1)
        box(cells, 2., 2.5, 2.2, 2.7)
        g = as_grid(cells)
        result = plan_approach(g, (2., 1.), (2.1, 2.6), LIVE)
        self.assertTrue(result.ok, result.reason)  # the rover side of the closed wall is in range
        self.assertLess(result.points[-1][1], 2.)
        sealed = room()
        box(sealed, 0., 0., 4., 4., value=OCCUPIED)
        box(sealed, .2, .2, .8, .8, value=FREE)  # the rover's own pocket, far from the target
        g = as_grid(sealed)
        direct = plan_path(g, (.5, .5), (3., 3.), LIVE)
        self.assertEqual(plan_approach(g, (.5, .5), (3., 3.), LIVE), direct)

    def test_free_target_is_planned_exactly_like_the_default(self):
        cells = room()
        box(cells, 1.8, 1.8, 2.2, 2.2)
        g = as_grid(cells)
        for target in ((1., 3.), (3.2, 1.)):
            for config in (PlannerConfig(), LIVE):
                self.assertEqual(plan_approach(g, (2., .5), target, config), plan_path(g, (2., .5), target, config))

    def test_non_goal_refusals_are_not_rerouted(self):
        start_in_wall = room()
        box(start_in_wall, 0., 0., 1., 1.)
        g = as_grid(start_in_wall)
        direct = plan_path(g, (.5, .5), (3., 3.), LIVE)
        self.assertEqual(direct.reason, 'start_blocked')
        self.assertEqual(plan_approach(g, (.5, .5), (3., 3.), LIVE), direct)


if __name__ == '__main__':
    unittest.main()
