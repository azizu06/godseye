"""Explore a room by unmapped coverage while routing around a central sign."""
import time
import unittest

import numpy as np

from backend.navigation import path_blocked
from backend.navigator import Navigator, NavSettings, planning_grid
from backend.occupancy import OccupancySnapshot
from backend.prototype import PrototypeActuation


def room(cells):
    snapshot = OccupancySnapshot(('classroom', 1), 1, time.monotonic(), (), .28,
                                 (-3., -3.), .05, cells, 0., True)
    navigator = Navigator(NavSettings(follower=PrototypeActuation().follower()),
                          pose=lambda: None, occupancy=lambda: snapshot,
                          submit=lambda *_: True, stop=lambda _: None,
                          publish=lambda _: None, armed_mode=lambda: 'explore')
    return snapshot, navigator


class RoomExplorationTests(unittest.TestCase):
    def test_sign_is_routed_around_while_scanning_forward(self):
        cells = np.zeros((120, 120), np.uint8)
        cells[:3] = cells[-3:] = 2
        cells[:, :3] = cells[:, -3:] = 2
        cells[30:90, 30:90] = 1
        cells[70:80, 57:63] = 2  # sign standing in the direct forward lane
        snapshot, navigator = room(cells)
        kind, _, goal, plan = navigator._plan(lambda: snapshot, (0., 0.), None, True, yaw=0.)
        self.assertEqual(kind, 'plan')
        self.assertTrue(plan.ok, plan.reason)
        self.assertGreater(goal[1], 1.)
        self.assertTrue(any(abs(x) > .5 for x, z in plan.points if .4 < z < 1.2))
        grid, config = planning_grid(snapshot, navigator.settings)
        self.assertFalse(path_blocked(grid, plan.points, config))

    def test_open_room_prefers_large_unmapped_region_over_tiny_forward_gap(self):
        cells = np.ones((120, 120), np.uint8)
        cells[:3] = cells[-3:] = 2
        cells[:, :3] = cells[:, -3:] = 2
        cells[100:110, 55:65] = 0  # small forward patch
        cells[20:100, 10:40] = 0  # broad unexplored side of classroom
        snapshot, navigator = room(cells)
        kind, _, goal, plan = navigator._plan(lambda: snapshot, (0., 0.), None, True, yaw=0.)
        self.assertEqual(kind, 'plan')
        self.assertTrue(plan.ok, plan.reason)
        self.assertLess(goal[0], -.8)

    def test_fully_mapped_walled_room_finishes_explore(self):
        cells = np.ones((120, 120), np.uint8)
        cells[:3] = cells[-3:] = 2
        cells[:, :3] = cells[:, -3:] = 2
        snapshot, navigator = room(cells)
        outcome = navigator._plan(lambda: snapshot, (0., 0.), None, True, yaw=0.)
        self.assertEqual(outcome[0], 'explore_complete')


if __name__ == '__main__':
    unittest.main()
