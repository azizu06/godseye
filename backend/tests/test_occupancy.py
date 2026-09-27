"""Depth points -> 2D occupancy grid, pure and over the real /live transport; no hardware."""
import base64
import json
import math
import threading
import time
import unittest
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from backend.app import MAP_MAX_AGE_S, create_app
from backend.mapping import build_point_chunk
from backend.occupancy import (CELL_M, FLOOR_TOL_M, HALF_EXTENT_M, OBSTACLE_MAX_M, OBSTACLE_MIN_M,
                               PUBLISH_INTERVAL_S, OccupancyGrid)
from backend.tests.test_map_transport import fresh, hello, next_of, wait_for
from backend.tests.test_pose_freshness import pose
from backend.tests.test_mapping import bundle

SESSION = ('occ-session', 1)
FLOOR_Y = -1.2  # a handheld phone: AR origin about 1.2 m above the floor


def plane(x0, x1, z0, z1, y, step=.02):
    xs, zs = np.meshgrid(np.arange(x0, x1, step), np.arange(z0, z1, step))
    return np.stack([xs.ravel(), np.full(xs.size, y), zs.ravel()], axis=1)


def box(x0, x1, z0, z1, y0, y1, step=.02):
    """The four vertical faces and the top of an axis-aligned box."""
    ys = np.arange(y0, y1, step)
    faces = []
    for x in (x0, x1):
        zz, yy = np.meshgrid(np.arange(z0, z1, step), ys)
        faces.append(np.stack([np.full(zz.size, x), yy.ravel(), zz.ravel()], axis=1))
    for z in (z0, z1):
        xx, yy = np.meshgrid(np.arange(x0, x1, step), ys)
        faces.append(np.stack([xx.ravel(), yy.ravel(), np.full(xx.size, z)], axis=1))
    faces.append(plane(x0, x1, z0, z1, y1, step))
    return np.concatenate(faces)


def feed(grid, points, frames=3):
    for _ in range(frames):
        grid.add(points)


def decode(message):
    raw = base64.b64decode(message['cells'], validate=True)
    return np.frombuffer(raw, dtype=np.uint8).reshape(message['height'], message['width'])


def cell_at(message, x, z):
    """Dashboard semantics (Scene.tsx Map2D): column i % width grows along +x from
    origin[0], row floor(i / width) grows along +z from origin[1]."""
    col = math.floor((x - message['origin'][0]) / message['cell_m'])
    row = math.floor((z - message['origin'][1]) / message['cell_m'])
    assert 0 <= col < message['width'] and 0 <= row < message['height'], (x, z)
    return int(decode(message)[row, col])


class ClassificationTests(unittest.TestCase):
    def test_flat_floor_becomes_free_cells_and_sets_the_floor_height(self):
        grid = OccupancyGrid(SESSION)
        feed(grid, plane(0, 1, 0, 1, FLOOR_Y))
        message = grid.message_if_due(0.)
        cells = decode(message)
        self.assertAlmostEqual(message['floor_y'], FLOOR_Y, delta=.02)
        self.assertEqual(int((cells == 2).sum()), 0)
        self.assertGreaterEqual(int((cells == 1).sum()), 19 * 19)
        self.assertEqual(cell_at(message, .5, .5), 1)

    def test_box_of_obstacle_points_becomes_occupied_and_floor_around_it_stays_free(self):
        grid = OccupancyGrid(SESSION)
        # Asymmetric placement so a swapped x/z or flipped row order would fail.
        feed(grid, np.concatenate([plane(-1, 1, -1, 1, FLOOR_Y),
                                   box(.6, .8, -.7, -.5, FLOOR_Y, FLOOR_Y + .3)]))
        message = grid.message_if_due(0.)
        for x, z in [(.6, -.6), (.79, -.6), (.7, -.7), (.7, -.51)]:  # box faces
            self.assertEqual(cell_at(message, x, z), 2, (x, z))
        for x, z in [(-.6, .7), (-.6, -.6), (.7, .6), (0, 0)]:  # open floor, incl. the mirror spots
            self.assertEqual(cell_at(message, x, z), 1, (x, z))

    def test_occupied_wins_over_floor_seen_in_the_same_cell(self):
        grid = OccupancyGrid(SESSION)
        leg = np.array([[.325, FLOOR_Y + h, .325] for h in np.arange(.1, .5, .02)])
        feed(grid, np.concatenate([plane(0, 1, 0, 1, FLOOR_Y), leg]))
        self.assertEqual(cell_at(grid.message_if_due(0.), .325, .325), 2)

    def test_ceiling_and_high_overhangs_are_ignored(self):
        grid = OccupancyGrid(SESSION)
        ceiling = plane(-1, 2, -1, 2, FLOOR_Y + 2.4)  # extends beyond the seen floor
        shelf = plane(.2, .4, .2, .4, FLOOR_Y + OBSTACLE_MAX_M + .2)
        feed(grid, np.concatenate([plane(0, 1, 0, 1, FLOOR_Y), ceiling, shelf]))
        message = grid.message_if_due(0.)
        cells = decode(message)
        self.assertAlmostEqual(message['floor_y'], FLOOR_Y, delta=.02)
        self.assertEqual(int((cells == 2).sum()), 0)
        self.assertEqual(cell_at(message, .3, .3), 1)
        # Ceiling alone is no evidence: the grid only spans the seen floor.
        self.assertLessEqual(message['width'] * CELL_M, 1.05)
        self.assertLessEqual(message['height'] * CELL_M, 1.05)

    def test_single_outliers_and_near_floor_noise_do_not_mark_occupied(self):
        grid = OccupancyGrid(SESSION)
        floor = plane(0, 1, 0, 1, FLOOR_Y)
        rng = np.random.default_rng(3)
        for _ in range(8):
            # One stray obstacle-height point per frame, each in a different cell,
            # plus floor jitter inside the tolerance and a speck just under the obstacle band.
            stray = np.array([[rng.uniform(0, 1), FLOOR_Y + rng.uniform(.2, 1.2), rng.uniform(0, 1)]])
            jitter = floor + np.column_stack([np.zeros(len(floor)),
                                              rng.uniform(-.015, .015, len(floor)),
                                              np.zeros(len(floor))])
            speck = np.array([[.5, FLOOR_Y + (FLOOR_TOL_M + OBSTACLE_MIN_M) / 2, .5]])
            grid.add(np.concatenate([jitter, stray, speck]))
        cells = decode(grid.message_if_due(0.))
        self.assertEqual(int((cells == 2).sum()), 0)

    def test_one_frame_of_floor_is_not_enough_to_call_a_cell_free(self):
        grid = OccupancyGrid(SESSION)
        grid.add(plane(0, 1, 0, 1, FLOOR_Y))
        self.assertIsNone(grid.message_if_due(0.))
        grid.add(plane(0, 1, 0, 1, FLOOR_Y))
        self.assertIsNotNone(grid.message_if_due(1.))

    def test_no_floor_or_no_points_publishes_nothing(self):
        grid = OccupancyGrid(SESSION)
        self.assertIsNone(grid.message_if_due(0.))
        grid.add(np.empty((0, 3)))
        self.assertIsNone(grid.message_if_due(1.))
        # A lone vertical wall has no horizontal plane to call the floor.
        wall = np.stack(np.meshgrid(np.arange(0, 1, .02), np.arange(-1, 0, .02)), -1).reshape(-1, 2)
        feed(grid, np.column_stack([wall[:, 0], wall[:, 1], np.full(len(wall), 2.)]))
        self.assertIsNone(grid.message_if_due(2.))


class BoundsTests(unittest.TestCase):
    def test_extent_is_capped_around_the_ar_origin_and_far_points_are_dropped(self):
        grid = OccupancyGrid(SESSION)
        far = np.array([[HALF_EXTENT_M + 3, FLOOR_Y, 0], [0, FLOOR_Y, -HALF_EXTENT_M - 2],
                        [0, 50., 0], [np.nan, FLOOR_Y, 0], [np.inf, 0, 0]])
        start = -HALF_EXTENT_M - 2 + CELL_M / 2  # one point per cell center
        huge = plane(start, HALF_EXTENT_M + 2, start, HALF_EXTENT_M + 2, FLOOR_Y, step=CELL_M)
        feed(grid, np.concatenate([huge, far]), frames=2)
        message = grid.message_if_due(0.)
        cap = round(2 * HALF_EXTENT_M / CELL_M)
        self.assertEqual((message['width'], message['height']), (cap, cap))
        self.assertEqual(message['origin'], [-HALF_EXTENT_M, -HALF_EXTENT_M])
        self.assertGreater(grid.dropped, 0)
        self.assertLessEqual(len(message['cells']), 400000)  # dashboard parser limit

    def test_a_frame_with_nothing_in_bounds_is_fresh_sensing_but_no_new_evidence(self):
        grid = OccupancyGrid(SESSION)
        grid.add(plane(0, 1, 0, 1, FLOOR_Y), now=4.)
        self.assertEqual((grid.revision, grid.accepted_at), (1, 4.))
        grid.add(np.array([[HALF_EXTENT_M + 3, FLOOR_Y, 0]]), now=5.)
        self.assertEqual((grid.revision, grid.accepted_at), (1, 5.))

    def test_voxel_store_is_bounded(self):
        grid = OccupancyGrid(SESSION, max_voxels=500)
        feed(grid, plane(0, 3, 0, 3, FLOOR_Y, step=CELL_M), frames=2)
        self.assertLessEqual(grid.voxels, 500)
        self.assertGreater(grid.dropped, 0)


class MessageTests(unittest.TestCase):
    def test_message_matches_the_v1_contract_and_is_scoped(self):
        grid = OccupancyGrid(SESSION)
        feed(grid, np.concatenate([plane(-.5, .5, -.5, .5, FLOOR_Y), box(0, .2, 0, .2, FLOOR_Y, FLOOR_Y + .3)]))
        message = grid.message_if_due(0.)
        self.assertEqual(set(message), {'version', 'type', 'session_id', 'map_epoch', 'origin', 'cell_m',
                                        'width', 'height', 'cells', 'floor_y'})
        self.assertEqual((message['version'], message['type']), (1, 'occupancy'))
        self.assertEqual((message['session_id'], message['map_epoch']), SESSION)
        self.assertEqual(message['cell_m'], .05)
        self.assertEqual(len(message['origin']), 2)
        self.assertTrue(all(isinstance(v, float) for v in message['origin']))
        self.assertTrue(isinstance(message['width'], int) and isinstance(message['height'], int))
        raw = base64.b64decode(message['cells'], validate=True)
        self.assertEqual(len(raw), message['width'] * message['height'])
        self.assertTrue(set(raw) <= {0, 1, 2} and {1, 2} <= set(raw))
        # origin is the min-x/min-z corner of cell (0, 0); cells snap to the 5 cm lattice.
        self.assertAlmostEqual(message['origin'][0], -.5, delta=1e-9)
        self.assertAlmostEqual(message['origin'][1], -.5, delta=1e-9)
        json.dumps(message, allow_nan=False)

    def test_publishes_at_most_once_per_second_and_only_on_change(self):
        grid = OccupancyGrid(SESSION)
        feed(grid, plane(0, 1, 0, 1, FLOOR_Y))
        self.assertIsNotNone(grid.message_if_due(10.))
        feed(grid, plane(1, 2, 0, 1, FLOOR_Y))  # the map grows
        self.assertIsNone(grid.message_if_due(10. + PUBLISH_INTERVAL_S / 2))
        grown = grid.message_if_due(10. + PUBLISH_INTERVAL_S)
        self.assertGreater(grown['width'], 20)
        self.assertIs(grid.last_message, grown)
        feed(grid, plane(0, 2, 0, 1, FLOOR_Y))  # more of the same evidence, same picture
        self.assertIsNone(grid.message_if_due(20.))
        self.assertLessEqual(PUBLISH_INTERVAL_S, 1.)
        self.assertGreaterEqual(PUBLISH_INTERVAL_S, 1.)


# --- Over the real transport ---------------------------------------------------------------

PITCH = math.radians(60)  # camera tilted down toward the floor
CAMERA_HEIGHT = 1.


def floor_rays(rows, cols):
    """Optical depth and world hit point of test_mapping-sized depth pixels (80x60 JPEG,
    fx = fy = 40, 20x15 depth) looking at the floor plane y = 0 from a camera 1 m up,
    pitched down 60 degrees; same geometry as mapping.py."""
    a = -PITCH
    u, v = (cols + .5) * 80 / 20, (rows + .5) * 60 / 15
    dx, dy = (u - 40) / 40, -(v - 30) / 40
    down = math.cos(a) * dy + math.sin(a)  # world y of the ray per meter of optical depth
    depth = CAMERA_HEIGHT / -down
    ahead = math.sin(a) * dy - math.cos(a)  # world z per meter of optical depth
    return depth, dx * depth, ahead * depth


def floor_frame(session='occ-live', frame_id=1, t_capture=1.):
    a = -PITCH
    transform = [1, 0, 0, 0, 0, math.cos(a), math.sin(a), 0, 0, -math.sin(a), math.cos(a), 0,
                 0, CAMERA_HEIGHT, 0, 1]
    rows, cols = np.mgrid[0:15, 0:20]
    depth = floor_rays(rows, cols)[0].astype('<f4')
    confidence = np.full(depth.shape, 2, dtype='u1')
    return fresh(bundle(depth, confidence, transform=transform, session=session),
                 frame_id=frame_id, t_capture=t_capture)


def send_frames(client, phone, session, count=3, first=1):
    # Repeats of the same view publish no points chunk (voxel dedupe) but still map.
    stats = client.app.state.map_stats
    for frame_id in range(first, first + count):
        target = stats['published'] + stats['no_new_points'] + 1
        phone.send_bytes(floor_frame(session, frame_id, float(frame_id)))
        wait_for(lambda: stats['published'] + stats['no_new_points'] >= target)


class LiveOccupancyTests(unittest.TestCase):
    def test_floor_frames_reach_live_viewers_as_an_occupancy_grid(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('occ-live'))
                send_frames(client, phone, 'occ-live')
                message = next_of(live, 'occupancy', limit=200)
            self.assertEqual((message['session_id'], message['map_epoch']), ('occ-live', 1))
            self.assertAlmostEqual(message['floor_y'], 0, delta=.03)
            cells = decode(message)
            self.assertGreater(int((cells == 1).sum()), 50)
            self.assertEqual(int((cells == 2).sum()), 0)
            # Floor sampled ahead of the camera (world -z, row 4 col 10 hits about (0.07, -1.06)).
            _, x, z = floor_rays(4, 10)
            self.assertLess(z, -1)
            self.assertEqual(cell_at(message, x, z), 1)
            self.assertFalse(client.app.state.armed)
            # A viewer that connects later receives the current grid with the snapshot.
            with client.websocket_connect('/live') as late:
                self.assertEqual(next_of(late, 'occupancy', limit=5)['cells'],
                                 client.app.state.occupancy.last_message['cells'])

    def test_grid_keeps_accumulating_when_repeated_frames_publish_no_points(self):
        # One frame is not enough evidence for a free cell (FREE_MIN_HITS = 2); the grid
        # only fills in if repeats reach it even though dedupe publishes nothing for them.
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('occ-repeat'))
                send_frames(client, phone, 'occ-repeat')
                message = next_of(live, 'occupancy', limit=200)
            stats = client.app.state.map_stats
            self.assertEqual((stats['published'], stats['no_new_points']), (1, 2))
            self.assertGreater(int((decode(message) == 1).sum()), 50)
            # Every accepted capture adds evidence, published points or not.
            self.assertEqual(client.app.state.occupancy.revision, 3)

    def test_map_reset_clears_the_grid_and_never_republishes_the_old_one(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('occ-old'))
                send_frames(client, phone, 'occ-old')
                next_of(live, 'occupancy', limit=200)
            wait_for(lambda: client.app.state.phone is None)
            self.assertEqual(client.post('/session').status_code, 200)
            grid = client.app.state.occupancy
            self.assertEqual(grid.session, client.app.state.session)
            self.assertIsNone(grid.last_message)
            self.assertEqual(grid.voxels, 0)
            with client.websocket_connect('/live') as late:
                seen = [late.receive_json() for _ in range(3)]  # health, objects, path
                seen.append(late.receive_json())  # then the next 2 Hz health, not a grid
                self.assertEqual([m['type'] for m in seen], ['health', 'objects', 'path', 'health'])
            client.post('/stop')
            messages = []
            while True:
                message = live.receive_json()
                messages.append(message)
                if message['type'] == 'health' and message['stop_reason'] == 'operator_stop':
                    break
            self.assertFalse(any(m['type'] == 'occupancy' for m in messages))

    def test_grid_computed_across_a_reset_is_discarded(self):
        started, release = threading.Event(), threading.Event()
        original = OccupancyGrid.message_if_due

        def gated(grid, now):
            message = original(grid, now)
            if message is not None and grid.session[0] == 'occ-stale':
                started.set()
                release.wait(5)
            return message

        with mock.patch.object(OccupancyGrid, 'message_if_due', gated), \
                TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('occ-stale'))
                send_frames(client, phone, 'occ-stale')
                self.assertTrue(started.wait(5))
                client.post('/session')  # map reset while the old grid is still computing
                release.set()
                wait_for(lambda: client.app.state.occupancy_stats['discarded_reset'] == 1)
            client.post('/stop')
            messages = []
            while True:
                message = live.receive_json()
                messages.append(message)
                if message['type'] == 'health' and message['stop_reason'] == 'operator_stop':
                    break
            self.assertFalse(any(m['type'] == 'occupancy' for m in messages))
            self.assertEqual(client.app.state.occupancy_stats['published'], 0)

    def test_slow_viewer_keeps_only_the_newest_grid(self):
        from backend.app import Listener
        with TestClient(create_app(':memory:')) as client:
            stalled = Listener()
            client.app.state.listeners.add(stalled)
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('occ-slow'))
                send_frames(client, phone, 'occ-slow')
                wait_for(lambda: client.app.state.occupancy_stats['published'] >= 1)
            self.assertEqual(stalled.occupancy['type'], 'occupancy')
            self.assertFalse(any(m['type'] == 'occupancy' for m in stalled.queue._queue))


class GatedMapping:
    """`build_points` whose first call waits for `release`, so a test can change the map meanwhile."""

    def __init__(self):
        self.started, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()

    def __call__(self, payload, session_id, map_epoch):
        first = not self.started.is_set()
        self.started.set()
        if first:
            self.release.wait(5)
        try:
            return build_point_chunk(payload, session_id, map_epoch)
        finally:
            if first:
                self.finished.set()

    def finish(self):
        """Release the held frame and give its worker thread time to run to the end."""
        self.release.set()
        assert self.finished.wait(5)
        time.sleep(.3)


def join(client, phone, session, epoch=1):
    """Say hello and wait until the backend owns this phone; returns its map's grid."""
    phone.send_json(hello(session, epoch))
    wait_for(lambda: client.app.state.phone is not None)
    return client.app.state.occupancy


def read_through_stop(client, live):
    """Every /live message up to a fresh operator-stop marker, which follows all earlier updates."""
    client.post('/stop')
    messages = []
    while not messages or messages[-1]['type'] != 'health' or messages[-1]['stop_reason'] != 'operator_stop':
        messages.append(live.receive_json())
    return messages


class LateMappingTests(unittest.TestCase):
    """A frame still mapping when it stops counting must leave every grid untouched.

    Its worker thread outlives the cancelled connection or the map reset, and a phone
    rejoining the same map resumes that map's grid, so evidence may only be committed
    for results accepted as current: same phone, same map, frame at most a second old.
    """

    def test_frame_finished_after_disconnect_never_reaches_the_resumed_grid(self):
        gate = GatedMapping()
        with TestClient(create_app(':memory:', build_points=gate)) as client, \
                client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                grid = join(client, phone, 'occ-late')
                phone.send_bytes(floor_frame('occ-late'))
                self.assertTrue(gate.started.wait(5))
            wait_for(lambda: client.app.state.phone is None)
            gate.finish()
            self.assertEqual(grid.voxels, 0)
            with client.websocket_connect('/phone') as phone:
                self.assertIs(join(client, phone, 'occ-late'), grid)  # the same map resumes its grid
                self.assertEqual(grid.voxels, 0)
                # The resumed grid still maps new captures; the late frame never publishes.
                send_frames(client, phone, 'occ-late', first=2)
                self.assertEqual(next_of(live, 'points')['frame_id'], 2)
                self.assertGreater(int((decode(next_of(live, 'occupancy', limit=200)) == 1).sum()), 50)
            self.assertEqual(grid.revision, 3)

    def test_frame_finished_after_reconnecting_to_a_new_epoch_touches_neither_grid(self):
        gate = GatedMapping()
        with TestClient(create_app(':memory:', build_points=gate)) as client, \
                client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                old = join(client, phone, 'occ-epoch')
                phone.send_bytes(floor_frame('occ-epoch'))
                self.assertTrue(gate.started.wait(5))
            wait_for(lambda: client.app.state.phone is None)
            with client.websocket_connect('/phone') as phone:
                new = join(client, phone, 'occ-epoch', epoch=2)
                self.assertIsNot(new, old)
                gate.finish()
                self.assertEqual((old.voxels, new.voxels), (0, 0))
                self.assertEqual((old.revision, new.revision), (0, 0))
                messages = read_through_stop(client, live)
            self.assertFalse(any(m['type'] in ('points', 'occupancy') for m in messages))
            self.assertEqual(client.app.state.map_stats['published'], 0)

    def test_frame_finished_after_a_map_reset_is_discarded_before_touching_either_grid(self):
        gate = GatedMapping()
        with TestClient(create_app(':memory:', build_points=gate)) as client, \
                client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                old = join(client, phone, 'occ-reset')
                phone.send_bytes(floor_frame('occ-reset'))
                self.assertTrue(gate.started.wait(5))
                # Revokes this phone mid-frame, but its mapping worker is still running.
                self.assertEqual(client.post('/session').status_code, 200)
                new = client.app.state.occupancy
                gate.release.set()
                wait_for(lambda: client.app.state.map_stats['discarded_reset'] == 1)
            self.assertEqual((old.voxels, new.voxels), (0, 0))
            self.assertEqual((old.revision, new.revision), (0, 0))
            self.assertEqual((old.accepted_at, new.accepted_at), (None, None))
            messages = read_through_stop(client, live)
            self.assertFalse(any(m['type'] in ('points', 'occupancy') for m in messages))
            self.assertEqual(client.app.state.map_stats['published'], 0)

    def test_frame_older_than_a_second_when_mapped_is_discarded_before_touching_the_grid(self):
        gate = GatedMapping()
        with TestClient(create_app(':memory:', build_points=gate)) as client, \
                client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                grid = join(client, phone, 'occ-old-frame')
                phone.send_bytes(floor_frame('occ-old-frame'))
                self.assertTrue(gate.started.wait(5))
                time.sleep(MAP_MAX_AGE_S + .1)
                gate.release.set()
                wait_for(lambda: client.app.state.map_stats['discarded_stale'] == 1)
                self.assertEqual(grid.voxels, 0)
                self.assertEqual((grid.revision, grid.accepted_at), (0, None))
                self.assertEqual(client.app.state.map_stats['published'], 0)
                # The link and the grid stay usable: the next fresh frame maps.
                phone.send_bytes(floor_frame('occ-old-frame', frame_id=2, t_capture=2.))
                self.assertEqual(next_of(live, 'points')['frame_id'], 2)
                self.assertGreater(grid.voxels, 0)
                self.assertEqual(grid.revision, 1)
                self.assertIsNotNone(grid.accepted_at)


    def test_frame_finished_after_tracking_loss_is_discarded_before_touching_the_grid(self):
        gate = GatedMapping()
        with TestClient(create_app(':memory:', build_points=gate)) as client:
            with client.websocket_connect('/phone') as phone:
                grid = join(client, phone, 'map-session')
                phone.send_bytes(floor_frame('map-session', t_capture=10.))
                self.assertTrue(gate.started.wait(5))
                phone.send_json(pose(10.1, 'not_available'))  # the frame's pose is now untrusted
                wait_for(lambda: client.app.state.tracking_lost_capture == 10.1)
                gate.release.set()
                wait_for(lambda: client.app.state.map_stats['discarded_tracking'] == 1)
                self.assertEqual(grid.voxels, 0)
                self.assertEqual((grid.revision, grid.accepted_at), (0, None))
                self.assertEqual(client.app.state.map_stats['published'], 0)


if __name__ == '__main__':
    unittest.main()


class FloorPersistenceTests(unittest.TestCase):
    def test_observed_floor_survives_peak_loss_but_obstacles_and_freshness_still_update(self):
        grid = OccupancyGrid(SESSION)
        feed(grid, plane(-1, 1, -1, 1, FLOOR_Y))
        first = grid.map_snapshot()
        self.assertIsNotNone(first.floor_y)
        original_time = first.accepted_at
        with mock.patch('backend.occupancy.estimate_floor', return_value=None):
            # Height memory does not pretend another sensor frame arrived.
            self.assertEqual(grid.map_snapshot().accepted_at, original_time)
            feed(grid, box(.3, .6, .3, .6, FLOOR_Y, FLOOR_Y + .4))
            current = grid.map_snapshot()
            self.assertEqual(current.floor_y, first.floor_y)
            self.assertNotIn('no_floor', current.blockers)
            self.assertEqual(current.cell(.45, .45), 2)
            self.assertIsNotNone(grid.snapshot()[2])
            self.assertIsNotNone(grid.message_if_due(10.))
            # A new AR epoch cannot inherit another map's floor height.
            reset = OccupancyGrid((SESSION[0], 2))
            feed(reset, plane(-1, 1, -1, 1, FLOOR_Y))
            self.assertIn('no_floor', reset.map_snapshot().blockers)
