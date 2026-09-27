"""A bounded current ARKit mesh augments depth without preserving departed geometry."""
import base64
import unittest

import numpy as np

from backend.frame_bundle import FrameValidationError, parse_frame_bundle
from backend.mapping import floor_plane_points
from backend.occupancy import FREE, OCCUPIED, OccupancyGrid, frame_evidence
from backend.prototype import prototype_geometry
from backend.tests.test_localization import bundle, fixture


def mesh_bundle(points):
    header, jpeg, depth, confidence = fixture()
    header['version'] = 3
    header['floor'] = dict(y=1.8, polygon=[[.5, 2.5], [1.5, 2.5],
                                           [1.5, 3.5], [.5, 3.5]])
    header['mesh_voxels'] = base64.b64encode(np.asarray(points, '<i2').tobytes()).decode()
    return bundle(header, jpeg, depth, confidence)


class LiveMeshTests(unittest.TestCase):
    def test_v3_decodes_bounded_world_voxels(self):
        frame = parse_frame_bundle(mesh_bundle([[20, 42, 60]]),
                                   session_id='synthetic', map_epoch=2)
        np.testing.assert_allclose(frame.mesh_points, [[1., 2.1, 3.]])

    def test_current_mesh_obstacle_disappears_on_empty_snapshot(self):
        frame = parse_frame_bundle(mesh_bundle([[20, 42, 60]]),
                                   session_id='synthetic', map_epoch=2)
        floor = floor_plane_points(frame)
        grid = OccupancyGrid(('synthetic', 2), calibration=prototype_geometry(.2286, .127))
        for now in (1., 2.):
            grid.commit(frame_evidence(floor, camera_y=2., camera_xz=(1., 3.),
                                       floor_y=frame.floor.y), now,
                        mesh_keys=frame_evidence(frame.mesh_points).keys)
        self.assertEqual(grid.map_snapshot().cell(1., 3.), OCCUPIED)
        empty = parse_frame_bundle(mesh_bundle([]), session_id='synthetic', map_epoch=2)
        grid.commit(frame_evidence(floor, camera_y=2., camera_xz=(1., 3.),
                                   floor_y=frame.floor.y), 3.,
                    mesh_keys=frame_evidence(empty.mesh_points).keys)
        self.assertEqual(grid.map_snapshot().cell(1., 3.), FREE)

    def test_current_mesh_obstacle_overrides_accumulated_floor(self):
        frame = parse_frame_bundle(mesh_bundle([[20, 42, 60]]),
                                   session_id='synthetic', map_epoch=2)
        floor = frame_evidence(floor_plane_points(frame), camera_y=2.,
                               camera_xz=(1., 3.), floor_y=frame.floor.y)
        grid = OccupancyGrid(('synthetic', 2), calibration=prototype_geometry(.2286, .127))
        for now in range(1, 61):
            grid.commit(floor, float(now))
        self.assertEqual(grid.map_snapshot().cell(1., 3.), FREE)
        grid.commit(floor, 61., mesh_keys=frame_evidence(frame.mesh_points).keys)
        self.assertEqual(grid.map_snapshot().cell(1., 3.), OCCUPIED)

    def test_mesh_disappearance_does_not_erase_independent_depth_obstacle(self):
        frame = parse_frame_bundle(mesh_bundle([[20, 42, 60]]),
                                   session_id='synthetic', map_epoch=2)
        floor = floor_plane_points(frame)
        obstacle = np.array([[1., 2.1, 3.]])
        grid = OccupancyGrid(('synthetic', 2), calibration=prototype_geometry(.2286, .127))
        for now in (1., 2., 3.):
            grid.commit(frame_evidence(np.concatenate((floor, obstacle)), camera_y=2.,
                                       camera_xz=(1., 3.), floor_y=frame.floor.y), now,
                        mesh_keys=frame_evidence(frame.mesh_points).keys)
        grid.commit(frame_evidence(floor, camera_y=2., camera_xz=(1., 3.),
                                   floor_y=frame.floor.y), 4.,
                    mesh_keys=frame_evidence(np.empty((0, 3))).keys)
        self.assertEqual(grid.map_snapshot().cell(1., 3.), OCCUPIED)

    def test_mesh_expires_when_new_frames_arrive_without_another_snapshot(self):
        frame = parse_frame_bundle(mesh_bundle([[20, 42, 60]]),
                                   session_id='synthetic', map_epoch=2)
        floor = floor_plane_points(frame)
        grid = OccupancyGrid(('synthetic', 2), calibration=prototype_geometry(.2286, .127))
        grid.commit(frame_evidence(floor, camera_y=2., camera_xz=(1., 3.),
                                   floor_y=frame.floor.y), 1.,
                    mesh_keys=frame_evidence(frame.mesh_points).keys)
        grid.commit(frame_evidence(floor, camera_y=2., camera_xz=(1., 3.),
                                   floor_y=frame.floor.y), 2.)
        self.assertEqual(grid.map_snapshot().cell(1., 3.), OCCUPIED)
        grid.commit(frame_evidence(floor, camera_y=2., camera_xz=(1., 3.),
                                   floor_y=frame.floor.y), 3.6)
        self.assertEqual(grid.map_snapshot().cell(1., 3.), FREE)

    def test_cached_mesh_outside_current_view_is_filtered_without_losing_depth(self):
        frame = parse_frame_bundle(mesh_bundle([[20, 42, 60], [20, 73, 60],
                                                [400, 42, 60]]),
                                   session_id='synthetic', map_epoch=2)
        np.testing.assert_allclose(frame.mesh_points, [[1., 2.1, 3.]])
        self.assertGreater(len(frame.depth), 0)
        distant = parse_frame_bundle(mesh_bundle([[400, 42, 60]]),
                                     session_id='synthetic', map_epoch=2)
        self.assertEqual(distant.mesh_points.shape, (0, 3))
        self.assertGreater(len(distant.depth), 0)

    def test_rejects_oversize_mesh(self):
        with self.assertRaises(FrameValidationError):
            parse_frame_bundle(mesh_bundle(np.zeros((4001, 3), np.int16)),
                               session_id='synthetic', map_epoch=2)
