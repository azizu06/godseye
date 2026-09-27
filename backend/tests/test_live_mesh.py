"""A bounded current ARKit mesh augments depth without preserving departed geometry."""
import base64
import unittest

import numpy as np
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.frame_bundle import FrameValidationError, parse_frame_bundle
from backend.mapping import floor_plane_points
from backend.occupancy import FREE, OCCUPIED, OccupancyGrid, frame_evidence
from backend.navigator import recoverable_start
from backend.prototype import prototype_geometry, filter_prototype_self_mesh
from backend.tests.test_localization import bundle, fixture
from backend.tests.test_map_transport import fresh, hello, wait_for
from backend.tests.test_objects import FakeDetector, LEFT_CUP


def mesh_bundle(points):
    header, jpeg, depth, confidence = fixture()
    header['version'] = 3
    header['floor'] = dict(y=1.8, polygon=[[.5, 2.5], [1.5, 2.5],
                                           [1.5, 3.5], [.5, 3.5]])
    header['mesh_voxels'] = base64.b64encode(np.asarray(points, '<i2').tobytes()).decode()
    return bundle(header, jpeg, depth, confidence)


class LiveMeshTests(unittest.TestCase):
    def test_outlier_mesh_keeps_live_mapping_and_detection_fresh(self):
        header, jpeg, depth, confidence = fixture()
        header.update(version=3, mesh_voxels=base64.b64encode(
            np.asarray([[20, 42, 60], [20, 73, 60]], '<i2').tobytes()).decode())
        depth[:] = 2
        confidence[:] = 2
        payload = bundle(header, jpeg, depth, confidence)
        with TestClient(create_app(':memory:', detector=FakeDetector(LEFT_CUP),
                                   calibration=prototype_geometry(.2286, .127),
                                   capture_directory='')) as client, \
                client.websocket_connect('/phone') as phone:
            phone.send_json(hello('synthetic', epoch=2))
            phone.send_bytes(fresh(payload))
            state = client.app.state
            wait_for(lambda: state.occupancy is not None and
                     state.occupancy.accepted_at is not None and state.detected_at is not None)
            self.assertEqual(state.map_stats['rejected'], 0)
            self.assertEqual(state.detect_stats['rejected'], 0)
            self.assertGreater(state.occupancy.voxels, 0)
            self.assertGreater(state.chunk_id, 0)
            self.assertEqual(client.get('/health').json()['detector'], 'ok')

    def test_prototype_self_mesh_clears_rear_chassis_but_keeps_forward_and_side_obstacles(self):
        frame = parse_frame_bundle(mesh_bundle([[24, 42, 60], [28, 42, 60],
                                                [12, 42, 60], [24, 42, 68]]),
                                   session_id='synthetic', map_epoch=2)
        kept = filter_prototype_self_mesh(frame.mesh_points, frame.transform,
                                          prototype_geometry(.2286, .127))
        np.testing.assert_allclose(kept, [[.6, 2.1, 3.], [1.2, 2.1, 3.4]])

    def test_near_out_of_view_mesh_does_not_block_a_clear_live_camera(self):
        frame = parse_frame_bundle(mesh_bundle([[16, 42, 60], [24, 42, 65],
                                                [24, 42, 70]]),
                                   session_id='synthetic', map_epoch=2)
        kept = filter_prototype_self_mesh(frame.mesh_points, frame.transform,
                                          prototype_geometry(.2286, .127),
                                          intrinsics=frame.intrinsics,
                                          image_size=frame.image.size)
        # The near sideways mesh anchor is outside today's camera image. A
        # forward sign and a farther wall remain navigation obstacles.
        np.testing.assert_allclose(kept, [[.8, 2.1, 3.], [1.2, 2.1, 3.5]])

    def test_departed_side_mesh_no_longer_blocks_prototype_start(self):
        frame = parse_frame_bundle(mesh_bundle([[19, 42, 63]]),
                                   session_id='synthetic', map_epoch=2)
        geometry = prototype_geometry(.2286, .127)
        floor = frame_evidence(floor_plane_points(frame), camera_y=2.,
                               camera_xz=(1., 3.), floor_y=frame.floor.y)
        filtered = filter_prototype_self_mesh(frame.mesh_points, frame.transform,
                                               geometry, intrinsics=frame.intrinsics,
                                               image_size=frame.image.size)
        for mesh, expected in ((frame.mesh_points, False), (filtered, True)):
            with self.subTest(filtered=expected):
                grid = OccupancyGrid(('synthetic', 2), calibration=geometry)
                for now in (1., 2., 3.):
                    grid.commit(floor, now, mesh_keys=frame_evidence(mesh).keys)
                snapshot = grid.map_snapshot()
                self.assertEqual(snapshot.traversable(1., 3.), expected)
                self.assertEqual(recoverable_start(snapshot, 1., 3., .1524), expected)

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
