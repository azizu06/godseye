"""/phone bundle -> bounded /live points chunk, over the real transport."""
import asyncio
import json
import struct
import threading
import time
import unittest
import unittest.mock

import numpy as np
from fastapi.testclient import TestClient

from backend.app import MAP_PENDING_POINTS, Listener, create_app
from backend.mapping import build_point_chunk, POINTS_PROTOCOL
from backend.point_dedupe import PointSettings
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
    def test_dense_viewer_negotiates_binary_while_legacy_viewer_keeps_bounded_json(self):
        with TestClient(create_app(':memory:', capture_directory='', point_settings=PointSettings(voxel_m=0))) as client:
            with client.websocket_connect('/live', subprotocols=[POINTS_PROTOCOL]) as dense, \
                    client.websocket_connect('/live') as legacy, client.websocket_connect('/phone') as phone:
                self.assertEqual(dense.accepted_subprotocol, POINTS_PROTOCOL)
                self.assertIsNone(legacy.accepted_subprotocol)
                phone.send_json(hello())
                depth = np.full((192, 256), 2, dtype='<f4')
                confidence = np.full(depth.shape, 2, dtype='u1')
                phone.send_bytes(fresh(raw_bundle(depth, confidence, session='map-session')))
                while True:
                    message = dense.receive()
                    if message.get('bytes') is not None:
                        packet = message['bytes']
                        break
                size = struct.unpack_from('<I', packet)[0]
                header = json.loads(packet[4:4 + size])
                self.assertEqual((header['version'], header['count'], header['session_id']),
                                 (2, 20_000, 'map-session'))
                old = next_of(legacy, 'points')
                self.assertEqual((old['version'], len(old['positions'])), (1, 7500))
                self.assertEqual(old['frame_id'], header['frame_id'])
                self.assertFalse(client.get('/health').json()['armed'])

    def test_phone_reconnect_preserves_chunk_ids_within_the_same_map(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            for frame_id in (1, 2):
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())
                    # A different wall each time: identical geometry would carry no new voxels.
                    phone.send_bytes(frame(frame_id=frame_id, t_capture=float(frame_id), depth=1. + frame_id))
                    chunk = next_of(live, 'points')
                    self.assertEqual(chunk['chunk_id'], frame_id)
                    self.assertEqual(chunk['session_id'], 'map-session')
                wait_for(lambda: client.app.state.phone is None)
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello(epoch=2))
                phone.send_bytes(fresh(frame(frame_id=3, t_capture=3.), map_epoch=2))
                chunk = next_of(live, 'points')
                self.assertEqual((chunk['chunk_id'], chunk['map_epoch']), (1, 2))

    def test_reset_flushes_old_map_control_messages_for_a_slow_live_viewer(self):
        # Hold the actual /live sender at its initial path so old-map updates
        # accumulate in the backend, rather than merely slowing the test reader.
        blocked, release = threading.Event(), threading.Event()
        app = create_app(':memory:')

        async def slow_live(scope, receive, send):
            async def gated_send(message):
                if scope.get('path') == '/live' and message['type'] == 'websocket.send':
                    body = json.loads(message['text'])
                    if body['type'] == 'path' and not blocked.is_set():
                        blocked.set()
                        while not release.is_set():
                            await asyncio.sleep(.01)
                await send(message)
            await app(scope, receive, gated_send)

        with TestClient(slow_live) as client, client.websocket_connect('/live') as live:
            self.assertTrue(blocked.wait(5))
            try:
                with client.websocket_connect('/phone') as old:
                    old.send_json(hello('old-session'))
                    old.send_bytes(frame('old-session'))
                    wait_for(lambda: app.state.map_stats['published'] == 1)
                wait_for(lambda: app.state.phone is None)
                self.assertEqual(client.post('/session').status_code, 200)
                with client.websocket_connect('/phone') as new:
                    new.send_json(hello('new-session'))
                    new.send_bytes(frame('new-session', frame_id=7))
                    wait_for(lambda: app.state.map_stats['published'] == 2)
                    release.set()
                    messages = []
                    # /stop publishes a terminal marker after every map update;
                    # read through it so stale controls after a point are caught.
                    client.post('/stop')
                    while True:
                        message = live.receive_json()
                        messages.append(message)
                        if message['type'] == 'health' and message['stop_reason'] == 'operator_stop':
                            break
            finally:
                release.set()
            self.assertFalse(any(m.get('session_id') == 'old-session' for m in messages))
            self.assertEqual(sum(m['type'] == 'pose' for m in messages), 1)
            self.assertEqual([m['frame_id'] for m in messages if m['type'] == 'points'], [7])
            self.assertTrue(any(m['type'] == 'objects' and m['session_id'] == 'new-session'
                                for m in messages))
            self.assertTrue(any(m['type'] == 'health' and m['stop_reason'] == 'session_reset'
                                for m in messages))

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
                # Points are deliberately published on a 250 ms cadence, the
                # same length as the pose-health window. A fresh pose proves
                # the phone link survived without depending on scheduler timing.
                phone.send_json(dict(version=1, type='pose', session_id='map-session',
                                     map_epoch=1, frame_id=3, t_capture=3.,
                                     t_wall_ms=int(time.time() * 1000),
                                     transform=TRANSFORM, tracking='normal'))
                wait_for(lambda: client.app.state.pose.t_capture == 3.)
                self.assertEqual(client.get('/health').json()['phone'], 'ok')

    def test_burst_is_coalesced_to_bounded_newest_work(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                for i in range(1, 21):
                    phone.send_bytes(frame(frame_id=i, t_capture=float(i), depth=1. + i / 10))
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
                    phone.send_bytes(frame(frame_id=i, t_capture=float(i), depth=1. + i / 10))
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


class PointDedupeTransportTests(unittest.TestCase):
    """Chunks carry newly observed voxels; map identity changes and new viewers start over."""

    def send_and_settle(self, client, phone, frame_id, depth=2.):
        stats = client.app.state.map_stats
        done = stats['published'] + stats['no_new_points']
        phone.send_bytes(frame(frame_id=frame_id, t_capture=float(frame_id), depth=depth))
        wait_for(lambda: stats['published'] + stats['no_new_points'] == done + 1)

    def test_repeated_view_sends_nothing_and_a_new_view_continues_the_chunk_ids(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.send_and_settle(client, phone, 1)
                self.send_and_settle(client, phone, 2)
                stats = client.app.state.map_stats
                self.assertEqual((stats['published'], stats['no_new_points']), (1, 1))
                self.send_and_settle(client, phone, 3, depth=3.)
                chunks = [next_of(live, 'points'), next_of(live, 'points')]
            self.assertEqual([(c['chunk_id'], c['frame_id']) for c in chunks], [(1, 1), (2, 3)])

    def test_session_reset_forgets_sent_voxels(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('first-map'))
                phone.send_bytes(frame('first-map'))
                next_of(live, 'points')
            wait_for(lambda: client.app.state.phone is None)
            self.assertEqual(client.post('/session').status_code, 200)
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('second-map'))
                phone.send_bytes(frame('second-map', frame_id=2, t_capture=2.))  # same wall
                chunk = next_of(live, 'points')
            self.assertEqual((chunk['session_id'], chunk['chunk_id']), ('second-map', 1))

    def test_reconnect_to_the_same_map_keeps_sent_voxels(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live'):
            for frame_id in (1, 2):
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())
                    self.send_and_settle(client, phone, frame_id)
                wait_for(lambda: client.app.state.phone is None)
            stats = client.app.state.map_stats
            self.assertEqual((stats['published'], stats['no_new_points']), (1, 1))

    def test_a_new_viewer_does_not_make_the_map_resend_to_everyone(self):
        # A dashboard opened later fills in as surfaces are newly seen or pass the refresh age.
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live'):
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.send_and_settle(client, phone, 1)
                with client.websocket_connect('/live') as second:
                    second.receive_json()  # registered once its first health arrives
                    self.send_and_settle(client, phone, 2)
            stats = client.app.state.map_stats
            self.assertEqual((stats['published'], stats['no_new_points']), (1, 1))

    def test_points_per_chunk_comes_from_the_environment(self):
        with unittest.mock.patch.dict('os.environ', {'GODSEYE_POINTS_PER_CHUNK': '40'}):
            app = create_app(':memory:')
        with TestClient(app) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame())
                self.assertEqual(len(next_of(live, 'points')['positions']), 40 * 3)

    def test_bad_point_settings_stop_startup(self):
        with unittest.mock.patch.dict('os.environ', {'GODSEYE_POINT_VOXEL_M': 'lots'}):
            with self.assertRaisesRegex(ValueError, 'GODSEYE_POINT_VOXEL_M'):
                create_app(':memory:')

    def test_disabled_dedupe_resends_every_frame(self):
        with TestClient(create_app(':memory:', point_settings=PointSettings(voxel_m=0))) as client, \
                client.websocket_connect('/live'):
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.send_and_settle(client, phone, 1)
                self.send_and_settle(client, phone, 2)
            self.assertEqual(client.app.state.map_stats['published'], 2)


if __name__ == '__main__':
    unittest.main()
