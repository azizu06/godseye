"""Object memory: stable IDs, raw observations, restart and session safety; no weights."""
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.localization import Detection, LocalizedDetection, localize_all
from backend.objects import ObjectMemory, associate
from backend.tests.test_map_transport import frame, hello, next_of, wait_for

SCHEMA = (Path(__file__).parents[1] / 'schema.sql').read_text()
WIRE_KEYS = {'id', 'class', 'position', 'confidence', 'first_seen', 'last_seen', 'observations', 'state', 'identity'}

# The test scene is a flat wall 2 m ahead of a camera at (1, 2, 3) looking along world -X
# (test_mapping.TRANSFORM). An 80x60 JPEG box centred at column u lands at world
# (-1, 2, 3 - (u - 40) / 20); these boxes are 0.4 m apart, inside the match radius.
LEFT_CUP = Detection((32, 24, 40, 36), 'cup', .8)  # world (-1, 2, 3.2)
RIGHT_CUP = Detection((40, 24, 48, 36), 'cup', .6)  # world (-1, 2, 2.8)
FAR_CHAIR = Detection((4, 24, 16, 36), 'chair', .9)  # world (-1, 2, 4.5)


class FakeDetector:
    """Stands in for MPSDetector: fixed boxes, real depth localization."""

    def __init__(self, *detections, gate=None):
        self.detections = list(detections)
        self.gate = gate
        self.calls = 0

    def localize(self, frame_bundle):
        self.calls += 1
        if self.gate is not None:
            self.gate()
        return localize_all(frame_bundle, self.detections)


class Broken:
    def localize(self, frame_bundle):
        raise RuntimeError('model exploded')


def located(detection, position, frame_id=1, session=('s', 1)):
    return LocalizedDetection(detection, position, 2., 6, *session, frame_id, float(frame_id))


def rows(client, table):
    """Count rows on the app's own event-loop thread, which owns the SQLite connection."""
    return client.portal.call(lambda: client.app.state.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])


def open_db(*sessions, frames=3):
    """In-memory schema with the session and frame rows observations must reference."""
    db = sqlite3.connect(':memory:')
    db.executescript(SCHEMA)
    for session in sessions or [('s', 1)]:
        db.execute('INSERT INTO sessions(session_id,map_epoch,created_at_ms) VALUES(?,?,1)', session)
        for frame_id in range(1, frames + 1):
            db.execute('INSERT INTO frames(session_id,map_epoch,frame_id,t_capture,t_wall_ms,transform_json,'
                       "tracking) VALUES(?,?,?,?,?,'[]','normal')", (*session, frame_id, frame_id, frame_id))
    db.commit()
    return db


class AssociationTests(unittest.TestCase):
    def test_same_class_within_radius_matches_other_classes_and_far_ones_do_not(self):
        tracks = [('cup-1', 'cup', (0, 0, 0))]
        found = [located(LEFT_CUP, (.2, 0, 0)), located(FAR_CHAIR, (0, 0, 0)), located(RIGHT_CUP, (2, 0, 0))]
        self.assertEqual(associate(tracks, found), ['cup-1', None, None])

    def test_two_same_class_detections_in_one_frame_never_share_an_object(self):
        tracks = [('cup-1', 'cup', (0, 0, 0))]
        found = [located(LEFT_CUP, (.3, 0, 0)), located(RIGHT_CUP, (.1, 0, 0))]
        self.assertEqual(associate(tracks, found), [None, 'cup-1'])  # the closer one keeps the id

    def test_each_detection_takes_its_nearest_free_object(self):
        tracks = [('a', 'cup', (0, 0, 0)), ('b', 'cup', (.4, 0, 0))]
        found = [located(LEFT_CUP, (.35, 0, 0)), located(RIGHT_CUP, (.05, 0, 0))]
        self.assertEqual(associate(tracks, found), ['b', 'a'])


class ObjectMemoryTests(unittest.TestCase):
    def test_repeated_sightings_keep_one_id_and_every_raw_observation(self):
        db = open_db()
        memory = ObjectMemory(db)
        self.assertTrue(memory.record(('s', 1), 1, 10., [located(LEFT_CUP, (1, 0, 0))]))
        self.assertTrue(memory.record(('s', 1), 2, 11., [located(LEFT_CUP, (1.2, 0, 0), 2)]))
        [cup] = memory.snapshot(('s', 1))
        self.assertEqual(set(cup), WIRE_KEYS)
        self.assertEqual((cup['observations'], cup['first_seen'], cup['last_seen'], cup['state']),
                         (2, 10., 11., 'present'))
        self.assertEqual(cup['position'], [1.1, 0, 0])
        raw = db.execute('SELECT frame_id, position_json, object_id FROM observations ORDER BY frame_id').fetchall()
        self.assertEqual(raw, [(1, '[1, 0, 0]', cup['id']), (2, '[1.2, 0, 0]', cup['id'])])
        self.assertFalse(memory.record(('s', 1), 3, 12., []))

    def test_objects_are_isolated_by_session_and_epoch(self):
        sessions = [('s', 1), ('s', 2), ('t', 1)]
        memory = ObjectMemory(open_db(*sessions))
        for session in sessions:  # the same cup at the same place in three maps
            memory.record(session, 1, 1., [located(LEFT_CUP, (0, 0, 0), session=session)])
        snapshots = [memory.snapshot(session) for session in sessions]
        self.assertEqual([len(objects) for objects in snapshots], [1, 1, 1])
        self.assertEqual(len({objects[0]['id'] for objects in snapshots}), 3)
        self.assertEqual(memory.snapshot(('missing', 1)), [])


class LiveObjectTests(unittest.TestCase):
    def feed(self, client, phone, frame_ids, session='map-session'):
        stats = client.app.state.detect_stats
        for frame_id in frame_ids:
            done = stats['published']
            phone.send_bytes(frame(session, frame_id=frame_id, t_capture=float(frame_id)))
            wait_for(lambda: stats['published'] == done + 1)

    def test_repeated_sightings_publish_one_stable_versioned_object(self):
        detector = FakeDetector(LEFT_CUP, FAR_CHAIR)
        with TestClient(create_app(':memory:', detector=detector)) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.feed(client, phone, [1, 2, 3])
                messages = [next_of(live, 'objects')]
                while not messages[-1]['objects'] or messages[-1]['objects'][0]['observations'] < 3:
                    messages.append(next_of(live, 'objects'))
                rest = client.get('/objects').json()
                health = client.get('/health').json()
        last = messages[-1]
        self.assertEqual((last['version'], last['session_id'], last['map_epoch']), (1, 'map-session', 1))
        self.assertEqual(rest, {k: v for k, v in last.items() if k != 'type'})
        by_class = {o['class']: o for o in last['objects']}
        self.assertEqual(set(by_class), {'cup', 'chair'})
        self.assertTrue(all(set(o) == WIRE_KEYS for o in last['objects']))
        cup = by_class['cup']
        self.assertEqual({o['id'] for m in messages for o in m['objects'] if o['class'] == 'cup'}, {cup['id']})
        self.assertEqual((cup['observations'], cup['state'], cup['confidence']), (3, 'present', .8))
        for actual, expected in zip(cup['position'], [-1, 2, 3.2]):
            self.assertAlmostEqual(actual, expected, places=3)
        self.assertLessEqual(cup['first_seen'], cup['last_seen'])
        self.assertEqual(health['detector'], 'ok')
        self.assertFalse(health['armed'])

    def test_same_class_neighbours_in_one_frame_stay_two_objects(self):
        detector = FakeDetector(LEFT_CUP)
        with TestClient(create_app(':memory:', detector=detector)) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.feed(client, phone, [1])
                [left] = client.get('/objects').json()['objects']
                # A second cup appears 0.4 m away, within the match radius of the first.
                detector.detections = [LEFT_CUP, RIGHT_CUP]
                self.feed(client, phone, [2, 3])
                objects = client.get('/objects').json()['objects']
            observations = rows(client, 'observations')
        by_id = {o['id']: o for o in objects}
        self.assertEqual(len(by_id), 2)
        self.assertEqual(by_id.pop(left['id'])['observations'], 3)
        [right] = by_id.values()
        self.assertEqual(right['observations'], 2)
        self.assertAlmostEqual(right['position'][2], 2.8, places=3)
        self.assertEqual(observations, 5)

    def test_new_session_gets_new_ids_and_hides_the_old_map(self):
        with TestClient(create_app(':memory:', detector=FakeDetector(LEFT_CUP))) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('session-a'))
                self.feed(client, phone, [1], 'session-a')
                [old] = client.get('/objects').json()['objects']
            reset = client.post('/session').json()
            self.assertEqual(client.get('/objects').json()['objects'], [])
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('session-b'))
                self.feed(client, phone, [1], 'session-b')
                body = client.get('/objects').json()
            stored = rows(client, 'objects')
        self.assertNotEqual(reset['session_id'], 'session-a')
        [new] = body['objects']
        self.assertEqual((body['session_id'], new['observations']), ('session-b', 1))
        self.assertNotEqual(new['id'], old['id'])
        self.assertEqual(stored, 2)

    def test_objects_survive_a_backend_restart_and_keep_their_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'godseye.db')
            with TestClient(create_app(path, detector=FakeDetector(LEFT_CUP))) as client:
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())
                    self.feed(client, phone, [1, 2])
                before = client.get('/objects').json()
            with TestClient(create_app(path, detector=FakeDetector(LEFT_CUP))) as client:
                self.assertEqual(client.get('/objects').json(), before)  # no phone connected yet
                with client.websocket_connect('/live') as live:
                    self.assertEqual(next_of(live, 'objects')['objects'], before['objects'])
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())  # same AR session resumes its map
                    self.feed(client, phone, [3])
                    [cup] = client.get('/objects').json()['objects']
        self.assertEqual(cup['id'], before['objects'][0]['id'])
        self.assertEqual(cup['observations'], 3)

    def test_inference_finishing_after_reset_or_disconnect_is_discarded(self):
        started, release = threading.Event(), threading.Event()

        def gate():
            started.set()
            release.wait(5)

        detector = FakeDetector(LEFT_CUP, gate=gate)
        with TestClient(create_app(':memory:', detector=detector)) as client:
            stats = client.app.state.detect_stats
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('old-session'))
                phone.send_bytes(frame('old-session'))
                self.assertTrue(started.wait(5))
                client.post('/session')  # map reset while inference is still running
                release.set()
                wait_for(lambda: stats['discarded_reset'] == 1)
            started.clear()
            release.clear()
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('next-session'))
                phone.send_bytes(frame('next-session'))
                self.assertTrue(started.wait(5))
            wait_for(lambda: client.get('/health').json()['phone'] == 'down')
            release.set()
            time.sleep(.3)  # the worker was cancelled with the connection; nothing may land
            self.assertEqual(stats['published'], 0)
            self.assertEqual(rows(client, 'objects'), 0)
            self.assertEqual(client.get('/objects').json()['objects'], [])

    def test_inference_queued_behind_the_model_is_skipped_once_its_phone_left(self):
        started, release = threading.Event(), threading.Event()

        def gate():
            started.set()
            release.wait(5)

        detector = FakeDetector(LEFT_CUP, gate=gate)
        with TestClient(create_app(':memory:', detector=detector)) as client:
            stats = client.app.state.detect_stats
            with client.websocket_connect('/phone') as slow:
                slow.send_json(hello('slow-session'))
                slow.send_bytes(frame('slow-session'))
                self.assertTrue(started.wait(5))  # this inference holds the model
            with client.websocket_connect('/phone') as flapping:
                flapping.send_json(hello('flapping-session'))
                flapping.send_bytes(frame('flapping-session'))
                wait_for(lambda: client.get('/health').json()['phone'] == 'ok')
            wait_for(lambda: client.get('/health').json()['phone'] == 'down')
            release.set()
            time.sleep(.3)
            self.assertEqual(detector.calls, 1)  # the queued run never reached the model
            self.assertEqual(stats['published'], 0)
            self.assertEqual(rows(client, 'objects'), 0)

    def test_detector_failure_keeps_points_pose_and_the_phone_link(self):
        with TestClient(create_app(':memory:', detector=Broken())) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone, self.assertLogs('backend.app', 'ERROR'):
                phone.send_json(hello())
                phone.send_bytes(frame())
                wait_for(lambda: client.app.state.detect_stats['failed'] == 1)
                self.assertEqual(next_of(live, 'pose')['tracking'], 'normal')
                self.assertEqual(next_of(live, 'points')['frame_id'], 1)
                health = client.get('/health').json()
        self.assertEqual((health['detector'], health['armed']), ('stale', False))
        self.assertEqual(client.post('/arm').status_code, 409)

    def test_burst_is_coalesced_to_bounded_inference(self):
        detector = FakeDetector(LEFT_CUP)
        with TestClient(create_app(':memory:', detector=detector)) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                for i in range(1, 21):
                    phone.send_bytes(frame(frame_id=i, t_capture=float(i)))
                wait_for(lambda: client.app.state.detect_stats['published'] >= 1)
                time.sleep(1.2)  # long enough for every remaining 2 Hz run to finish
                [cup] = client.get('/objects').json()['objects']
                stats = client.app.state.detect_stats
        self.assertGreater(stats['replaced'], 10)
        self.assertLess(detector.calls, 5)
        self.assertEqual(cup['observations'], stats['published'])


if __name__ == '__main__':
    unittest.main()


class IndoorClassFilterTests(unittest.TestCase):
    def test_default_indoor_set_drops_outdoor_classes(self):
        from backend.detector import INDOOR_CLASSES, detector_classes_from_env
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('GODSEYE_DETECTOR_CLASSES', None)
            classes = detector_classes_from_env()
        self.assertIs(classes, INDOOR_CLASSES)
        self.assertIn('person', classes)
        self.assertIn('chair', classes)
        for junk in ('elephant', 'banana', 'giraffe', 'car', 'toilet'):
            self.assertNotIn(junk, classes)

    def test_env_all_and_custom_list(self):
        from backend.detector import detector_classes_from_env
        with mock.patch.dict(os.environ, {'GODSEYE_DETECTOR_CLASSES': 'all'}):
            self.assertIsNone(detector_classes_from_env())
        with mock.patch.dict(os.environ, {'GODSEYE_DETECTOR_CLASSES': 'person, chair'}):
            self.assertEqual(detector_classes_from_env(), frozenset({'person', 'chair'}))

    def test_snapshot_hides_stored_objects_outside_visible_classes(self):
        db = sqlite3.connect(':memory:')
        db.executescript(Path(__file__).parents[1].joinpath('schema.sql').read_text())
        db.execute("INSERT INTO sessions (session_id, map_epoch, created_at_ms) VALUES ('s', 1, 0)")
        for i, name in enumerate(('person', 'elephant', 'chair')):
            db.execute('INSERT INTO objects (id, session_id, map_epoch, class, position_json, identity_confidence, '
                       "first_seen, last_seen, observations, state) VALUES (?, 's', 1, ?, '[0,0,0]', .9, ?, ?, 1, 'present')",
                       (f'o{i}', name, i, i))
        shown = ObjectMemory(db, visible_classes=frozenset({'person', 'chair'})).snapshot(('s', 1))
        self.assertEqual(sorted(o['class'] for o in shown), ['chair', 'person'])
        self.assertEqual(len(ObjectMemory(db).snapshot(('s', 1))), 3)
