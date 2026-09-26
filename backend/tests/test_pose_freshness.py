"""Only validated, advancing sensor poses may refresh phone health.

Receipt is not progress: a replayed capture with fresh send wall times, a
non-rigid transform or a stale delayed bundle must never keep `phone: ok`,
and nothing here may arm the (logging-only) drive.
"""
import json
import struct
import threading
import time
import unittest

import numpy as np
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.frame_bundle import FrameValidationError
from backend.mapping import build_point_chunk
from backend.tests.test_map_transport import frame, hello, next_of, wait_for
from backend.tests.test_mapping import TRANSFORM

IDENTITY = [1., 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
INTERVAL_S = .08  # eight sends span 0.56 s, well past the 250 ms pose gate


def pose(t_capture, tracking='normal', transform=None, frame_id=None):
    return dict(version=1, type='pose', session_id='map-session', map_epoch=1,
                frame_id=int(t_capture * 100) if frame_id is None else frame_id,
                t_capture=t_capture, t_wall_ms=int(time.time() * 1000), tracking=tracking,
                transform=list(IDENTITY if transform is None else transform))


def with_wall_time(payload, t_wall_ms):
    length = struct.unpack_from('<I', payload)[0]
    header = json.loads(payload[4:4 + length])
    header['t_wall_ms'] = t_wall_ms
    encoded = json.dumps(header).encode()
    return struct.pack('<I', len(encoded)) + encoded + payload[4 + length:]


def phone_health(client):
    return client.get('/health').json()


def assert_rejected(test, client, phone):
    """The backend closed the socket (1008); it must not hang open on bad input."""
    wait_for(lambda: client.app.state.phone is None, timeout=2.)
    with test.assertRaises(WebSocketDisconnect) as closed:
        phone.receive_json()
    test.assertEqual(closed.exception.code, 1008)


def assert_never_armed(test, client):
    health = phone_health(client)
    test.assertFalse(health['armed'])
    test.assertEqual(client.post('/arm').status_code, 409)
    test.assertFalse(client.app.state.armed)


class ReplayedPoseTests(unittest.TestCase):
    def test_counterfactual_replayed_capture_with_fresh_wall_times_is_stale(self):
        """Old path: `phone: ok` after 0.56 s of replays. Fixed path: stale, watchdog stop."""
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                for _ in range(8):
                    phone.send_json(pose(1.0))  # same frame/capture, fresh send wall time
                    time.sleep(INTERVAL_S)
                wait_for(lambda: client.app.state.pose is not None)
                health = phone_health(client)
                self.assertEqual(health['phone'], 'stale', health)
                self.assertGreater(health['pose_age_ms'], 250)
                self.assertEqual(health['stop_reason'], 'pose_stale')
                assert_never_armed(self, client)

    def test_valid_advancing_poses_keep_the_phone_ok(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                for step in range(8):
                    phone.send_json(pose(1.0 + step * .08))
                    time.sleep(INTERVAL_S)
                self.assertEqual(phone_health(client)['phone'], 'ok')
                # Progress is real, but healthy telemetry alone still never arms the car.
                assert_never_armed(self, client)

    def test_replayed_bundle_with_fresh_wall_times_is_stale_and_mapped_once(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                for _ in range(8):
                    phone.send_bytes(frame(frame_id=1, t_capture=1.))
                    time.sleep(INTERVAL_S)
                self.assertEqual(next_of(live, 'points')['frame_id'], 1)
                self.assertEqual(phone_health(client)['phone'], 'stale')
                stats = client.app.state.map_stats
                self.assertEqual(stats['published'], 1)
                self.assertEqual(stats['received'], 1)
                self.assertEqual(stats['discarded_order'], 7)
                assert_never_armed(self, client)

    def test_backwards_pose_neither_refreshes_nor_rewinds(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.1))
                wait_for(lambda: client.app.state.pose is not None)
                received = client.app.state.pose_at
                phone.send_json(pose(10.0))
                wait_for(lambda: client.app.state.stop_reason == 'pose_stale')
                self.assertEqual(client.app.state.pose.t_capture, 10.1)
                self.assertEqual(client.app.state.pose_at, received)
                assert_never_armed(self, client)


class RigidTransformTests(unittest.TestCase):
    BAD = {
        'zero': [0.] * 16,
        'scaled': [2., 0, 0, 0, 0, 2, 0, 0, 0, 0, 2, 0, 0, 0, 0, 1],
        'reflected': [-1., 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        'projective': [1., 0, 0, .5, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        'sheared': [1., 0, 0, 0, .3, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    }

    def test_counterfactual_non_rigid_transform_is_rejected_not_healthy(self):
        """Old path: an all-zero transform yielded `phone: ok`."""
        for name, transform in self.BAD.items():
            with self.subTest(name), TestClient(create_app(':memory:')) as client:
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())
                    phone.send_json(pose(1.0, transform=transform))
                    assert_rejected(self, client, phone)
                self.assertIsNone(client.app.state.pose)
                self.assertNotEqual(phone_health(client)['phone'], 'ok')
                assert_never_armed(self, client)

    def test_non_rigid_binary_frame_is_rejected_before_any_state_changes(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                payload = frame(frame_id=1, t_capture=1.)
                length = struct.unpack_from('<I', payload)[0]
                header = json.loads(payload[4:4 + length])
                header['transform'] = [0.] * 16
                encoded = json.dumps(header).encode()
                phone.send_bytes(struct.pack('<I', len(encoded)) + encoded + payload[4 + length:])
                assert_rejected(self, client, phone)
            self.assertEqual(client.app.state.capture.count, 0)
            self.assertEqual(client.app.state.map_stats['published'], 0)

    def test_valid_rigid_transforms_pass_the_shared_validator(self):
        from backend.frame_bundle import validate_rigid_transform
        for transform in (IDENTITY, TRANSFORM):
            np.testing.assert_allclose(validate_rigid_transform(transform).shape, (4, 4))
        for name, transform in self.BAD.items():
            with self.subTest(name), self.assertRaises(FrameValidationError):
                validate_rigid_transform(transform)
        with self.assertRaises(FrameValidationError):
            validate_rigid_transform([float('nan')] + IDENTITY[1:])
        with self.assertRaises(FrameValidationError):
            validate_rigid_transform(IDENTITY[:15])

    def test_rotated_pose_is_accepted(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(1.0, transform=TRANSFORM))
                wait_for(lambda: client.app.state.pose is not None)
                self.assertEqual(phone_health(client)['phone'], 'ok')


class DelayedBundleTests(unittest.TestCase):
    def test_delayed_bundle_maps_with_its_own_capture_without_refreshing_pose(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.1))
                next_of(live, 'pose')
                received = client.app.state.pose_at
                phone.send_bytes(frame(frame_id=1000, t_capture=10.0))
                chunk = next_of(live, 'points')
                # The bundle's own rotated transform, not the newer pose at the origin.
                np.testing.assert_allclose(np.array(chunk['positions']).reshape(-1, 3)[:, 0], -1)
                self.assertEqual(chunk['t_capture'], 10.0)
                self.assertEqual(client.app.state.pose.t_capture, 10.1)
                self.assertEqual(client.app.state.pose_at, received)
                self.assertEqual(client.get('/capture/status').json()['mapping']['published'], 1)
                phone.send_bytes(frame(frame_id=1005, t_capture=10.05, depth=3.))  # new geometry: not deduped away
                self.assertEqual(next_of(live, 'points')['frame_id'], 1005)
                self.assertEqual(client.app.state.pose_at, received)
                self.assertEqual(phone_health(client)['phone'], 'stale')
                assert_never_armed(self, client)

    def test_bundle_older_than_the_map_age_is_dropped(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.0))
                phone.send_bytes(frame(t_capture=8.))
                wait_for(lambda: client.app.state.map_stats['discarded_order'] == 1)
                self.assertEqual(client.app.state.map_stats['published'], 0)

    def test_stale_wall_time_bundle_is_discarded_without_stopping_a_healthy_phone(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.0))
                wait_for(lambda: client.app.state.pose is not None)
                phone.send_bytes(with_wall_time(frame(t_capture=10.05), 0))
                wait_for(lambda: client.app.state.map_stats['discarded_wall_time'] == 1)
                self.assertEqual(client.app.state.map_stats['published'], 0)
                self.assertNotEqual(client.app.state.stop_reason, 'pose_stale')


class SameFrameTests(unittest.TestCase):
    def test_pose_then_matching_bundle_maps_once_without_double_progress(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.0, transform=TRANSFORM, frame_id=7))
                next_of(live, 'pose')
                received = client.app.state.pose_at
                phone.send_bytes(frame(frame_id=7, t_capture=10.0))
                self.assertEqual(next_of(live, 'points')['frame_id'], 7)
                self.assertEqual(client.app.state.pose_at, received)
                stats = client.app.state.map_stats
                self.assertEqual((stats['received'], stats['published']), (1, 1))
                # The duplicate of that frame adds nothing.
                phone.send_bytes(frame(frame_id=7, t_capture=10.0))
                wait_for(lambda: stats['discarded_order'] == 1)
                self.assertEqual(stats['published'], 1)
                self.assertEqual(client.app.state.pose_at, received)

    def test_bundle_then_matching_pose_counts_progress_once(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame(frame_id=7, t_capture=10.0))
                next_of(live, 'points')
                received = client.app.state.pose_at
                phone.send_json(pose(10.0, transform=TRANSFORM, frame_id=7))
                time.sleep(.1)
                self.assertEqual(client.app.state.pose_at, received)
                self.assertEqual(client.app.state.map_stats['published'], 1)

    def test_inconsistent_same_frame_transform_is_rejected(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.0, transform=IDENTITY, frame_id=7))
                wait_for(lambda: client.app.state.pose is not None)
                phone.send_bytes(frame(frame_id=7, t_capture=10.0))  # TRANSFORM != IDENTITY
                assert_rejected(self, client, phone)
            self.assertEqual(client.app.state.map_stats['published'], 0)
            self.assertEqual(client.app.state.capture.count, 0)

    def test_inconsistent_same_frame_id_is_rejected(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.0, transform=TRANSFORM, frame_id=7))
                wait_for(lambda: client.app.state.pose is not None)
                phone.send_bytes(frame(frame_id=8, t_capture=10.0))
                assert_rejected(self, client, phone)
            self.assertEqual(client.app.state.map_stats['published'], 0)

    def test_same_capture_pose_with_different_tracking_is_rejected(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.0, transform=TRANSFORM, frame_id=7))
                wait_for(lambda: client.app.state.pose is not None)
                phone.send_json(pose(10.0, 'limited', transform=TRANSFORM, frame_id=7))
                assert_rejected(self, client, phone)


class TrackingLossTests(unittest.TestCase):
    def test_recovery_restores_health_but_never_rearms(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.0))
                phone.send_json(pose(10.1, 'limited'))
                wait_for(lambda: client.app.state.stop_reason == 'tracking_lost')
                self.assertEqual(phone_health(client)['phone'], 'stale')
                phone.send_json(pose(10.2))
                wait_for(lambda: phone_health(client)['phone'] == 'ok')
                self.assertEqual(client.app.state.stop_reason, 'tracking_lost')
                assert_never_armed(self, client)

    def test_replayed_limited_pose_does_not_recover_or_refresh(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.1, 'limited'))
                wait_for(lambda: client.app.state.stop_reason == 'tracking_lost')
                received = client.app.state.pose_at
                for _ in range(3):
                    phone.send_json(pose(10.1, 'limited'))
                time.sleep(.1)
                self.assertEqual(client.app.state.pose_at, received)

    def test_frames_captured_before_tracking_loss_are_not_mapped_after_recovery(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.1, 'limited'))
                next_of(live, 'pose')
                phone.send_json(pose(10.2))
                wait_for(lambda: client.app.state.pose.t_capture == 10.2)
                phone.send_bytes(frame(t_capture=10.05))
                wait_for(lambda: client.app.state.capture.count == 1)
                self.assertEqual(client.app.state.map_stats['published'], 0)
                self.assertEqual(client.app.state.map_stats['discarded_tracking'], 1)
                phone.send_bytes(frame(frame_id=2, t_capture=10.15))
                self.assertEqual(next_of(live, 'points')['frame_id'], 2)

    def test_inflight_result_is_discarded_if_tracking_was_lost(self):
        started, release = threading.Event(), threading.Event()

        def gated(payload, session_id, epoch):
            started.set()
            release.wait(5)
            return build_point_chunk(payload, session_id, epoch)

        with TestClient(create_app(':memory:', build_points=gated)) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame(t_capture=10))
                self.assertTrue(started.wait(5))
                phone.send_json(pose(10.1, 'not_available'))
                wait_for(lambda: client.app.state.tracking_lost_capture == 10.1)
                release.set()
                wait_for(lambda: client.app.state.map_stats['discarded_tracking'] == 1)
                self.assertEqual(client.app.state.map_stats['published'], 0)


class ReconnectTests(unittest.TestCase):
    def test_reconnect_resets_connection_progress_and_tracking_loss(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(500.0, 'limited'))
                wait_for(lambda: client.app.state.tracking_lost_capture == 500.0)
            wait_for(lambda: phone_health(client)['phone'] == 'down')
            # A restarted phone app has a fresh, much smaller uptime clock.
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(1.0))
                wait_for(lambda: phone_health(client)['phone'] == 'ok')
                phone.send_bytes(frame(frame_id=3, t_capture=1.5))
                self.assertEqual(next_of(live, 'points')['frame_id'], 3)
            self.assertEqual(client.app.state.stop_reason, 'phone_disconnected')
            assert_never_armed(self, client)

    def test_new_session_revokes_progress_and_disarmed_state_holds(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.0))
                wait_for(lambda: phone_health(client)['phone'] == 'ok')
                client.post('/session')
                self.assertEqual(phone_health(client)['phone'], 'down')
                self.assertIsNone(client.app.state.pose)
                phone.send_json(pose(11.0))
                assert_rejected(self, client, phone)
            assert_never_armed(self, client)


if __name__ == '__main__':
    unittest.main()
