"""/phone bundle -> bounded /live points chunk, over the real transport."""
import json
import struct
import threading
import time
import unittest

import numpy as np
from fastapi.testclient import TestClient

from backend.app import MAP_PENDING_POINTS, Listener, create_app
from backend.mapping import build_point_chunk
from backend.tests.test_mapping import TRANSFORM, bundle as raw_bundle, grids

MAX_POINTS = 2500


def fresh(payload, **header_overrides):
    """Restamp a test bundle with the current wall clock (phones must be within 250 ms)."""
    header_len = struct.unpack_from('<I', payload)[0]
    header = json.loads(payload[4:4 + header_len])
    header.update(t_wall_ms=int(time.time() * 1000), **header_overrides)
    encoded = json.dumps(header).encode()
    return struct.pack('<I', len(encoded)) + encoded + payload[4 + header_len:]


def frame(session='map-session', frame_id=1, t_capture=1., **grid):
    return fresh(raw_bundle(*grids(**grid), transform=TRANSFORM, session=session),
                 frame_id=frame_id, t_capture=t_capture)


def hello(session='map-session', epoch=1):
    return dict(version=1, type='hello', device='test-phone', session_id=session,
                map_epoch=epoch, supports_scene_depth=True, supports_mesh=False)


def pose(t_capture, tracking='normal'):
    return dict(version=1, type='pose', session_id='map-session', map_epoch=1,
                frame_id=int(t_capture * 100), t_capture=t_capture,
                t_wall_ms=int(time.time() * 1000), tracking=tracking,
                transform=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 10, 20, 30, 1])


def next_of(ws, wanted, limit=60):
    for _ in range(limit):
        message = ws.receive_json()
        if message['type'] == wanted:
            return message
    raise AssertionError('no live message of type ' + wanted)


def wait_for(predicate, timeout=5.):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('condition not reached')
        time.sleep(.01)


class MapTransportTests(unittest.TestCase):
    def test_encoder_latency_does_not_starve_points_or_rewind_latest_pose(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10.1))
                next_of(live, 'pose')
                received = client.app.state.pose_at
                phone.send_bytes(frame(frame_id=1000, t_capture=10.0))
                chunk = next_of(live, 'points')
                # Frame has its own rotated pose at (1,2,3), not the newer (10,20,30).
                np.testing.assert_allclose(np.array(chunk['positions']).reshape(-1, 3)[:, 0], -1)
                self.assertEqual(chunk['t_capture'], 10.0)
                self.assertEqual(client.app.state.pose.t_capture, 10.1)
                self.assertEqual(client.app.state.pose_at, received)
                status = client.get('/capture/status').json()
                self.assertEqual(status['position'], [10, 20, 30])
                self.assertEqual(status['mapping']['published'], 1)
                self.assertEqual(status['frame']['metadata']['frame_id'], 1000)
                # A second delayed bundle can advance the map without refreshing health.
                phone.send_bytes(frame(frame_id=1005, t_capture=10.05))
                self.assertEqual(next_of(live, 'points')['frame_id'], 1005)
                self.assertEqual(client.app.state.pose_at, received)
                self.assertEqual(client.get('/health').json()['phone'], 'stale')

    def test_equal_time_and_duplicate_bundles_never_refresh_pose_watchdog(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_json(pose(10))
                next_of(live, 'pose')
                received = client.app.state.pose_at
                phone.send_bytes(frame(frame_id=1000, t_capture=10))
                next_of(live, 'points')
                phone.send_bytes(frame(frame_id=1000, t_capture=10))
                wait_for(lambda: client.app.state.map_stats['discarded_order'] == 1)
                self.assertEqual(client.app.state.pose_at, received)
                self.assertEqual(client.app.state.map_stats['published'], 1)

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

    def test_one_bounded_chunk_with_known_world_geometry(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame())
                chunk = next_of(live, 'points')
                stats = client.app.state.map_stats
            self.assertEqual((chunk['version'], chunk['chunk_id']), (1, 1))
            self.assertEqual((chunk['session_id'], chunk['map_epoch'], chunk['frame_id']), ('map-session', 1, 1))
            positions = np.array(chunk['positions']).reshape(-1, 3)
            self.assertTrue(0 < len(positions) <= MAX_POINTS)
            self.assertEqual(len(chunk['colors']), len(chunk['positions']))
            # Flat 2 m wall, camera rotated 90 deg about Y at (1, 2, 3): every point has x = -1.
            np.testing.assert_allclose(positions[:, 0], -1, atol=1e-3)
            self.assertEqual(stats['published'], 1)

    def test_invalid_depth_is_rejected_and_the_phone_link_survives(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame(confidence=1))
                wait_for(lambda: client.app.state.map_stats['rejected'] == 1)
                self.assertEqual(client.app.state.map_stats['published'], 0)
                phone.send_bytes(frame(frame_id=2, t_capture=2.))
                self.assertEqual(next_of(live, 'points')['frame_id'], 2)
                self.assertEqual(client.get('/health').json()['phone'], 'ok')

    def test_burst_is_coalesced_to_bounded_newest_work(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                for i in range(1, 21):
                    phone.send_bytes(frame(frame_id=i, t_capture=float(i)))
                seen = []
                while not seen or seen[-1] != 20:
                    seen.append(next_of(live, 'points')['frame_id'])
                stats = client.app.state.map_stats
            self.assertLess(len(seen), 6)
            self.assertEqual(seen, sorted(seen))
            self.assertGreater(stats['replaced'], 10)

    def test_slow_viewer_backlog_is_bounded_and_does_not_evict_control_messages(self):
        with TestClient(create_app(':memory:')) as client:
            stalled = Listener()  # never drained, like a stuck browser tab
            with client.websocket_connect('/phone') as phone:
                client.app.state.listeners.add(stalled)
                phone.send_json(hello())
                for i in range(1, 6):
                    phone.send_bytes(frame(frame_id=i, t_capture=float(i)))
                    wait_for(lambda: client.app.state.map_stats['published'] == i)
                pending = list(stalled.points)
                control = list(stalled.queue._queue)
            self.assertEqual(len(pending), MAP_PENDING_POINTS)
            self.assertEqual([m['frame_id'] for m in pending], [4, 5])
            self.assertTrue(control and all(m['type'] != 'points' for m in control))

    def test_chunk_computed_across_reset_is_discarded_and_never_leaks_into_next_session(self):
        started, release = threading.Event(), threading.Event()

        def gated(payload, session_id, epoch):
            started.set()
            release.wait(5)
            return build_point_chunk(payload, session_id, epoch)

        with TestClient(create_app(':memory:', build_points=gated)) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as old:
                old.send_json(hello('old-session'))
                old.send_bytes(frame('old-session'))
                self.assertTrue(started.wait(5))
                client.post('/session')  # map reset while the old chunk is still computing
                release.set()
                wait_for(lambda: client.app.state.map_stats['discarded_reset'] == 1)
            started.clear()
            with client.websocket_connect('/phone') as new:
                new.send_json(hello('new-session'))
                new.send_bytes(frame('new-session', frame_id=7))
                chunk = next_of(live, 'points')
                self.assertEqual((chunk['session_id'], chunk['frame_id']), ('new-session', 7))
            self.assertEqual(client.app.state.map_stats['published'], 1)

    def test_chunk_computed_across_disconnect_is_discarded(self):
        started, release = threading.Event(), threading.Event()

        def gated(payload, session_id, epoch):
            started.set()
            release.wait(5)
            return build_point_chunk(payload, session_id, epoch)

        with TestClient(create_app(':memory:', build_points=gated)) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame())
                self.assertTrue(started.wait(5))
            wait_for(lambda: client.get('/health').json()['phone'] == 'down')
            release.set()
            time.sleep(.3)  # the worker was cancelled with the connection; nothing may arrive
            stats = client.app.state.map_stats
            self.assertEqual(stats['published'], 0)
            self.assertEqual(client.get('/health').json()['stop_reason'], 'phone_disconnected')

    def test_stale_or_out_of_order_frames_never_reach_the_map(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame(frame_id=2, t_capture=5.))
                self.assertEqual(next_of(live, 'points')['frame_id'], 2)
                phone.send_bytes(frame(frame_id=1, t_capture=4.))  # older capture: pose gate drops it
                stale = fresh(raw_bundle(*grids(), session='map-session'), frame_id=3, t_capture=6.)
                header_len = struct.unpack_from('<I', stale)[0]
                header = json.loads(stale[4:4 + header_len])
                header['t_wall_ms'] = 0  # captured long ago
                encoded = json.dumps(header).encode()
                phone.send_bytes(struct.pack('<I', len(encoded)) + encoded + stale[4 + header_len:])
                time.sleep(.6)
                self.assertEqual(client.app.state.map_stats['published'], 1)

    def test_drive_path_stays_disarmed_with_mapping_active(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame())
                next_of(live, 'points')
                health = client.get('/health').json()
                self.assertFalse(health['armed'])
                self.assertEqual((health['car'], health['detector']), ('down', 'down'))
                self.assertEqual(client.post('/arm').status_code, 409)


if __name__ == '__main__':
    unittest.main()
