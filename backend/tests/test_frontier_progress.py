"""Reached frontiers advance coverage without changing reachability rules."""
import math
import unittest

import numpy as np

from backend.navigation import Grid, PlannerConfig, nearest_frontier, preferred_explore_frontier
from backend.occupancy import FREE, OCCUPIED, UNKNOWN


class FrontierProgressTests(unittest.TestCase):
    def setUp(self):
        self.config = PlannerConfig(robot_radius_m=0., margin_m=0., start_snap_radius_m=0.)

    def selectors(self, grid, start, *, excluded=(), allow_unknown=False, config=None):
        config = config or self.config
        return (
            nearest_frontier(grid, start, config, allow_unknown=allow_unknown, excluded=excluded),
            preferred_explore_frontier(grid, start, 0., config,
                                       allow_unknown=allow_unknown, excluded=excluded),
        )

    def test_reached_boundary_regions_are_not_chosen_again(self):
        cells = np.full((20, 20), UNKNOWN, np.uint8)
        cells[6:14, 6:14] = FREE
        grid = Grid.from_array(cells, origin=(0., 0.), cell_m=.1)
        for selector in (nearest_frontier, preferred_explore_frontier):
            reached = []
            for _ in range(20):
                args = (grid, (1.05, 1.05), self.config) if selector == nearest_frontier else (
                    grid, (1.05, 1.05), 0., self.config)
                goal = selector(*args, excluded=tuple(reached))
                if goal is None:
                    break
                self.assertTrue(all(math.dist(goal, old) > .5 for old in reached))
                self.assertEqual(grid.cells[grid.world_to_cell(*goal)], FREE)
                reached.append(goal)
            else:
                self.fail('The unchanged boundary was revisited indefinitely')
            self.assertGreater(len(reached), 1)

    def test_large_minimum_frontier_distance_also_bounds_exclusion(self):
        cells = np.full((40, 40), UNKNOWN, np.uint8)
        cells[5:35, 5:35] = FREE
        grid = Grid.from_array(cells, origin=(0., 0.), cell_m=.1)
        config = PlannerConfig(robot_radius_m=0., margin_m=0., frontier_min_distance_m=.9)
        for first in self.selectors(grid, (2.05, 2.05), config=config):
            self.assertIsNotNone(first)
            for successor in self.selectors(grid, (2.05, 2.05), excluded=(first,), config=config):
                self.assertIsNotNone(successor)
                self.assertGreater(math.dist(first, successor), .9)

    def test_unknown_crossing_requires_explicit_opt_in_even_after_local_frontiers_are_reached(self):
        cells = np.full((20, 20), UNKNOWN, np.uint8)
        cells[7:13, 2:5] = FREE
        cells[7:13, 12:17] = FREE
        grid = Grid.from_array(cells, origin=(0., 0.), cell_m=.1)
        reached = tuple(grid.cell_center(r, c) for r in range(7, 13) for c in range(2, 5))
        self.assertEqual(self.selectors(grid, (.35, 1.05), excluded=reached), (None, None))
        for goal in self.selectors(grid, (.35, 1.05), excluded=reached, allow_unknown=True):
            self.assertIsNotNone(goal)
            self.assertGreaterEqual(goal[0], 1.25)
        cells[:, 8] = OCCUPIED
        sealed = Grid.from_array(cells, origin=(0., 0.), cell_m=.1)
        self.assertEqual(self.selectors(sealed, (.35, 1.05), excluded=reached,
                                        allow_unknown=True), (None, None))

    def test_diagonal_corner_gap_does_not_make_a_frontier_reachable(self):
        cells = np.full((5, 5), OCCUPIED, np.uint8)
        cells[1, 1] = cells[2, 2] = FREE
        cells[2, 3] = UNKNOWN
        grid = Grid.from_array(cells, origin=(0., 0.), cell_m=.2)
        self.assertEqual(self.selectors(grid, (.3, .3), allow_unknown=True), (None, None))


if __name__ == '__main__':
    unittest.main()
