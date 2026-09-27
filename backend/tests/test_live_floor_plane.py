"""A classified ARKit floor plane can seed free cells when glossy depth is unreliable."""
import unittest

import numpy as np

from backend.frame_bundle import FrameValidationError, parse_frame_bundle
from backend.mapping import floor_plane_points
from backend.occupancy import FREE, OCCUPIED, OccupancyGrid, frame_evidence
from backend.prototype import prototype_geometry
from backend.tests.test_localization import bundle, fixture


def plane_bundle(*, floor=None, version=2):
    header, jpeg, depth, confidence = fixture()
    header['version'] = version
    header['floor'] = floor or dict(y=1.8, polygon=[[.5, 2.5], [1.5, 2.5],
                                                    [1.5, 3.5], [.5, 3.5]])
    confidence[:] = 0  # The camera sees the glossy floor, LiDAR does not trust it.
    return bundle(header, jpeg, depth, confidence)


class LiveFloorPlaneTests(unittest.TestCase):
    def test_classified_floor_seeds_free_navigation_cells_without_depth(self):
        frame = parse_frame_bundle(plane_bundle(), session_id='synthetic', map_epoch=2)
        points = floor_plane_points(frame)
        self.assertGreater(len(points), 100)
        self.assertTrue(np.allclose(points[:, 1], 1.8))
        grid = OccupancyGrid(('synthetic', 2), calibration=prototype_geometry(.2286, .127))
        for now in (1., 2.):
            grid.commit(frame_evidence(points, camera_y=2., camera_xz=(1., 3.),
                                       floor_y=frame.floor.y), now)
        snapshot = grid.map_snapshot()
        self.assertTrue(snapshot.ready)
        self.assertEqual(snapshot.cell(1., 3.), FREE)
        self.assertAlmostEqual(snapshot.floor_y, 1.8)

    def test_rejects_unclassified_or_impossible_floor_metadata(self):
        bad = [
            (dict(y=2.1, polygon=[[.5, 2.5], [1.5, 2.5], [1.5, 3.5], [.5, 3.5]]), 2),
            (dict(y=1.8, polygon=[[.5, 2.5], [1.5, 2.5], [1.5, 2.5]]), 2),
            (dict(y=1.8, polygon=[[.5, 2.5], [1.5, 2.5], [1.5, 3.5], [.5, 3.5]]), 1),
        ]
        for floor, version in bad:
            with self.subTest(floor=floor, version=version), self.assertRaises(FrameValidationError):
                parse_frame_bundle(plane_bundle(floor=floor, version=version),
                                   session_id='synthetic', map_epoch=2)

    def test_obstacle_depth_wins_over_seeded_floor(self):
        frame = parse_frame_bundle(plane_bundle(), session_id='synthetic', map_epoch=2)
        floor = floor_plane_points(frame)
        obstacle = np.array([[1., 2.1, 3.], [1.01, 2.12, 3.01]])
        grid = OccupancyGrid(('synthetic', 2), calibration=prototype_geometry(.2286, .127))
        for now in (1., 2., 3.):
            grid.commit(frame_evidence(np.concatenate((floor, obstacle)), camera_y=2.,
                                       camera_xz=(1., 3.), floor_y=frame.floor.y), now)
        self.assertEqual(grid.map_snapshot().cell(1., 3.), OCCUPIED)
