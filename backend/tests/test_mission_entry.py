"""Measured recon-entry persistence through real REST/phone lifecycle, without hardware."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.drive import FakeCar
from backend.tests.test_map_transport import hello, wait_for
from backend.tests.test_objects import FakeDetector
from backend.tests.test_pose_freshness import pose, IDENTITY


class MissionEntryTests(unittest.TestCase):
    @contextmanager
    def rig(self, path=':memory:', car=None):
        app = create_app(path, detector=FakeDetector(), car=car or FakeCar(), capture_directory='')
        with TestClient(app) as client:
            # Record the real watchdog start boundary without running an unrelated planner.
            starts = []
            app.state.nav.start_explore = Mock(side_effect=lambda generation: starts.append(
                client.app.state.mission_entry))
            yield client, starts

    def ready(self, client, phone, capture=1., x=2., z=4.):
        transform = list(IDENTITY)
        transform[12:15] = [x, 1.25, z]  # measured camera translation, no inferred chassis/Y.
        phone.send_json(pose(capture, transform=transform))
        wait_for(lambda: client.app.state.pose is not None and client.app.state.pose.t_capture == capture)
        client.portal.call(lambda: setattr(client.app.state, 'detected_at', time.monotonic()))

    def explore(self, client):
        self.assertEqual(client.post('/mode', json={'mode': 'explore'}).status_code, 200)
        result = client.post('/arm')
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_successful_explore_records_exact_entry_before_watchdog_start(self):
        with self.rig() as (client, starts), client.websocket_connect('/phone') as phone:
            self.assertIsNone(client.get('/health').json()['mission_entry'])
            phone.send_json(hello())
            self.ready(client, phone)
            before = int(time.time() * 1000)
            result = self.explore(client)
            entry = result['mission_entry']
            self.assertEqual({k: v for k, v in entry.items() if k != 'started_at_ms'}, dict(
                session_id='map-session', map_epoch=1, start=[2., 4.], frame_id=100,
                t_capture=1., basis='explore_start'))
            self.assertGreaterEqual(entry['started_at_ms'], before)
            self.assertLessEqual(entry['started_at_ms'], int(time.time() * 1000))
            wait_for(lambda: bool(starts))
            self.assertEqual(starts[0], entry)
            with client.websocket_connect('/live') as live:
                self.assertEqual(live.receive_json()['mission_entry'], entry)

    def test_stop_mode_rearm_reconnect_and_restart_preserve_entry_and_other_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'session.db')
            with self.rig(path) as (client, _):
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())
                    self.ready(client, phone)
                    client.portal.call(lambda: client.app.state.db.execute(
                        "UPDATE sessions SET configuration_json=?", ('{"other":{"keep":true}}',)))
                    entry = self.explore(client)['mission_entry']
                    client.post('/stop')
                    self.ready(client, phone, 2., x=8., z=9.)
                    self.assertEqual(self.explore(client)['mission_entry'], entry)
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())
                    self.ready(client, phone, 3., x=11., z=12.)
                    self.assertEqual(self.explore(client)['mission_entry'], entry)
                config = client.portal.call(lambda: json.loads(client.app.state.db.execute(
                    'SELECT configuration_json FROM sessions').fetchone()[0]))
                self.assertEqual(config['other'], {'keep': True})
                self.assertEqual(config['mission_entry'], entry)
            with self.rig(path) as (client, _):
                self.assertEqual(client.get('/health').json()['mission_entry'], entry)
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello(epoch=2))
                    wait_for(lambda: client.app.state.session == ('map-session', 2))
                    self.assertIsNone(client.get('/health').json()['mission_entry'])
                client.post('/session')
                self.assertIsNone(client.get('/health').json()['mission_entry'])

    def test_failed_or_standard_arm_has_no_entry_and_pose_alone_never_creates_it(self):
        with self.rig() as (client, _), client.websocket_connect('/phone') as phone:
            phone.send_json(hello())
            wait_for(lambda: client.app.state.session is not None)
            client.post('/mode', json={'mode': 'explore'})
            self.assertEqual(client.post('/arm').status_code, 409)
            self.assertIsNone(client.get('/health').json()['mission_entry'])
            self.ready(client, phone)
            self.assertIsNone(client.get('/health').json()['mission_entry'])
            client.post('/mode', json={'mode': 'manual'})
            result = client.post('/arm')
            self.assertEqual(result.status_code, 200)
            self.assertIsNone(result.json()['mission_entry'])
            client.post('/stop')
            self.ready(client, phone, 2.)
            client.post('/mode', json={'mode': 'explore'})
            client.app.state.motion.car.error = RuntimeError('offline fixture refused zero')
            self.assertEqual(client.post('/arm').status_code, 409)
            self.assertIsNone(client.get('/health').json()['mission_entry'])
            client.app.state.motion.car.error = None
            self.ready(client, phone, 3., x=5., z=6.)
            self.assertEqual(self.explore(client)['mission_entry']['start'], [5., 6.])

    def test_storage_failure_does_not_change_arm_or_backfill_on_rearm_or_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'session.db')
            with self.rig(path) as (client, _), client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.ready(client, phone)
                with patch('backend.app.record_entry', side_effect=sqlite3.OperationalError('TEST unavailable')):
                    result = self.explore(client)
                self.assertTrue(result['armed'])
                self.assertIsNone(result['mission_entry'])
                client.post('/stop')
                self.ready(client, phone, 2., x=8., z=9.)
                self.assertIsNone(self.explore(client)['mission_entry'])
            with self.rig(path) as (client, _), client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.ready(client, phone, 3., x=11., z=12.)
                self.assertIsNone(self.explore(client)['mission_entry'])

    def test_existing_session_without_entry_is_not_backfilled_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'session.db')
            with self.rig(path) as (client, _), client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.ready(client, phone)
                self.assertIsNone(client.get('/health').json()['mission_entry'])
            with self.rig(path) as (client, _), client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.ready(client, phone, 2.)
                self.assertIsNone(self.explore(client)['mission_entry'])

    def test_relay_await_uses_final_pose_and_cancelled_start_records_nothing(self):
        import asyncio
        import threading
        from types import SimpleNamespace
        import numpy as np
        from backend.occupancy import OccupancySnapshot
        from backend.prototype import PrototypeActuation, prototype_geometry
        from backend.rover_relay import RelayCar

        key = 'TEST_KEY_NOT_REAL_01234567890123456789'
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                started, release = threading.Event(), threading.Event()
                car = RelayCar(key, PrototypeActuation())
                car.connected = True
                car.health = lambda: 'ok'
                car.blockers = lambda: []

                async def prepare(session):
                    started.set()
                    while not release.is_set():
                        await asyncio.sleep(.005)
                    car.armed_session = session.upper()

                car.prepare = prepare
                app = create_app(':memory:', car=car, detector=FakeDetector(),
                                 calibration=prototype_geometry(.24, .14), capture_directory='')
                with TestClient(app) as client, client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())
                    self.ready(client, phone)
                    snapshot = OccupancySnapshot(('map-session', 1), 1, time.monotonic(), (), .18,
                                                 (-1., -1.), .05, np.ones((40, 40), dtype=np.uint8), 0.)
                    app.state.occupancy = SimpleNamespace(session=('map-session', 1), map_snapshot=lambda: snapshot)
                    app.state.autonomy_map = snapshot
                    app.state.nav.start_explore = Mock()
                    headers = {'Authorization': 'Bearer ' + key}
                    client.post('/mode', json={'mode': 'explore'}, headers=headers)
                    responses = []
                    worker = threading.Thread(target=lambda: responses.append(client.post('/arm', headers=headers)))
                    worker.start()
                    self.assertTrue(started.wait(2))
                    try:
                        self.assertIsNone(client.get('/health').json()['mission_entry'])
                        self.ready(client, phone, 2., x=6., z=7.)
                        if cancel:
                            client.post('/stop')
                    finally:
                        release.set()
                        worker.join(2)
                    self.assertFalse(worker.is_alive())
                    if cancel:
                        self.assertEqual(responses[0].status_code, 409)
                        self.assertIsNone(client.get('/health').json()['mission_entry'])
                        self.ready(client, phone, 3., x=8., z=9.)
                        retry = client.post('/arm', headers=headers)
                        self.assertEqual(retry.status_code, 200, retry.text)
                        self.assertEqual(retry.json()['mission_entry']['start'], [8., 9.])
                    else:
                        self.assertEqual(responses[0].status_code, 200, responses[0].text)
                        entry = responses[0].json()['mission_entry']
                        self.assertEqual((entry['start'], entry['frame_id'], entry['t_capture']),
                                         ([6., 7.], 200, 2.))

    def test_entry_uses_current_profile_pose_age_policy_without_a_second_gate(self):
        from types import SimpleNamespace
        import numpy as np
        from backend.occupancy import OccupancySnapshot
        from backend.prototype import PrototypeActuation, prototype_geometry
        from backend.rover_relay import RelayCar

        key = 'TEST_KEY_NOT_REAL_01234567890123456789'
        for prototype in (False, True):
            with self.subTest(prototype=prototype):
                car = RelayCar(key, PrototypeActuation()) if prototype else FakeCar()
                if prototype:
                    car.connected = True
                    car.health = lambda: 'ok'
                    car.blockers = lambda: []

                    async def prepare(session):
                        car.armed_session = session.upper()

                    car.prepare = prepare
                app = create_app(':memory:', car=car, detector=FakeDetector(), capture_directory='',
                                 calibration=prototype_geometry(.24, .14) if prototype else None)
                with TestClient(app) as client, client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())
                    self.ready(client, phone, x=6., z=7.)
                    snapshot = OccupancySnapshot(('map-session', 1), 1, time.monotonic(), (), .18,
                                                 (-1., -1.), .05, np.ones((40, 40), dtype=np.uint8), 0.)
                    app.state.occupancy = SimpleNamespace(session=('map-session', 1), map_snapshot=lambda: snapshot)
                    app.state.autonomy_map = snapshot
                    app.state.nav.start_explore = Mock()
                    headers = {'Authorization': 'Bearer ' + key} if prototype else {}
                    client.post('/mode', json={'mode': 'explore'}, headers=headers)
                    client.portal.call(lambda: setattr(app.state, 'pose_at', time.monotonic() - .5))
                    result = client.post('/arm', headers=headers)
                    if prototype:
                        self.assertEqual(app.state.nav.settings.pose_max_age_s, 1.)
                        self.assertEqual(result.status_code, 200, result.text)
                        self.assertEqual(result.json()['mission_entry']['start'], [6., 7.])
                        self.assertEqual(result.json()['mission_entry']['frame_id'], 100)
                    else:
                        self.assertEqual(result.status_code, 409)
                        self.assertIsNone(client.get('/health').json()['mission_entry'])

    def test_same_process_reconnect_before_first_explore_can_record_real_start(self):
        with self.rig() as (client, _):
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.ready(client, phone)
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                self.ready(client, phone, 2., x=6., z=7.)
                self.assertEqual(self.explore(client)['mission_entry']['start'], [6., 7.])


class EntryStoreTests(unittest.TestCase):
    def test_insert_once_and_invalid_metadata_leave_unrelated_config_untouched(self):
        from backend.mission_entry import load_entry, record_entry
        entry = dict(session_id='map', map_epoch=1, start=[2., 4.], frame_id=3,
                     t_capture=2., started_at_ms=1234, basis='explore_start')
        for config in ('{"other":true}', '{bad json', '[]', '{"mission_entry":null,"other":true}',
                       '{"mission_entry":{"session_id":"wrong"},"other":true}', '{"other":NaN}'):
            with self.subTest(config=config), sqlite3.connect(':memory:') as db:
                db.execute('CREATE TABLE sessions(session_id TEXT,map_epoch INTEGER,configuration_json TEXT)')
                db.execute('INSERT INTO sessions VALUES(?,?,?)', ('map', 1, config))
                self.assertIsNone(load_entry(db, ('map', 1)))
                result = record_entry(db, ('map', 1), entry)
                saved = db.execute('SELECT configuration_json FROM sessions').fetchone()[0]
                if config == '{"other":true}':
                    self.assertEqual(result, entry)
                    self.assertTrue(json.loads(saved)['other'])
                    self.assertEqual(record_entry(db, ('map', 1), {**entry, 'start': [8., 9.]}), entry)
                    self.assertIsNone(load_entry(db, ('map', 2)))
                else:
                    self.assertIsNone(result)
                    self.assertEqual(saved, config)

    def test_unavailable_marker_is_insert_once_and_invalid_entry_is_not_exposed(self):
        from backend.mission_entry import load_entry, record_entry, valid_entry
        entry = dict(session_id='map', map_epoch=1, start=[2., 4.], frame_id=3,
                     t_capture=2., started_at_ms=1234, basis='explore_start')
        for changes in ({'start': [float('nan'), 4.]}, {'start': [True, 4.]},
                        {'start': [10**500, 4.]}, {'frame_id': -1}, {'frame_id': True},
                        {'map_epoch': 2}, {'basis': 'current_pose'}, {'t_capture': None}):
            self.assertIsNone(valid_entry({**entry, **changes}, ('map', 1)))
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE sessions(session_id TEXT,map_epoch INTEGER,configuration_json TEXT)')
            db.execute('INSERT INTO sessions VALUES(?,?,?)', ('map', 1, '{"other":true}'))
            self.assertIsNone(record_entry(db, ('map', 1), None))
            self.assertIsNone(record_entry(db, ('map', 1), entry))
            self.assertIsNone(load_entry(db, ('map', 1)))
            self.assertEqual(json.loads(db.execute('SELECT configuration_json FROM sessions').fetchone()[0]),
                             dict(other=True, mission_entry=None))
