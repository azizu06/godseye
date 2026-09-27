"""The navigation window follows the rover without exhausting the live wire budget."""
import unittest

import numpy as np

from backend.occupancy import OccupancyGrid, frame_evidence
from backend.tests.test_occupancy import FLOOR_Y, SESSION, plane


class RollingOccupancyTests(unittest.TestCase):
    def test_capacity_recovers_for_new_hallways_after_older_evidence_fills_store(self):
        grid = OccupancyGrid(SESSION, max_voxels=500)
        for x in (0., 150.):
            points = plane(x, x + 1., 0., 1., FLOOR_Y)
            for i in range(3):
                grid.commit(frame_evidence(points, camera_y=.5, camera_xz=(x + .5, .5)),
                            now=x + i)
        self.assertLessEqual(grid.voxels, 500)
        self.assertEqual(grid.map_snapshot().cell(150.5, .5), 1)
        self.assertGreater(grid.evicted, 0)

    def test_rover_can_map_and_plan_past_the_original_ten_meter_edge(self):
        grid = OccupancyGrid(SESSION)
        for step, x in enumerate((0., 9.5, 11., 18., 150.)):
            points = plane(x - .5, x + .5, -1., 1., FLOOR_Y)
            for i in range(3):
                grid.commit(frame_evidence(points, camera_y=.5, camera_xz=(x, 0.)), now=step * 5 + i)
            message = grid.message_if_due(step * 5 + 3)
            snapshot = grid.map_snapshot()
            self.assertEqual(snapshot.cell(x, 0.), 1)
            self.assertLessEqual(snapshot.cells.shape[0] * snapshot.cells.shape[1], 160_000)
            self.assertLessEqual(message['width'] * message['height'], 160_000)
            self.assertLessEqual(len(message['cells']), 400_000)
        self.assertGreater(snapshot.origin[0], 140.)
        # Returning to an earlier hallway recovers its stored world evidence.
        grid.commit(frame_evidence(np.empty((0, 3)), camera_xz=(0., 0.)), now=30.)
        self.assertEqual(grid.map_snapshot().cell(0., 0.), 1)


if __name__ == '__main__':
    unittest.main()
