"""Ordinary-obstruction API uses an existing fake drive session, never arm/prepare."""
from contextlib import contextmanager
from types import SimpleNamespace
import time
import unittest

from fastapi.testclient import TestClient
import numpy as np

from backend.app import create_app
from backend.drive import FakeCar
from backend.calibration import RoverCalibration
from backend.navigator import NavSettings
from backend.tests.test_objects import FakeDetector
from backend.tests.test_navigator import FakeOccupancy
from tools.car_rehearsal import TEST_CALIBRATION, wait_for


@contextmanager
def running_explore():
    car = FakeCar()
    app = create_app(db_path=':memory:', car=car, detector=FakeDetector(),
                     calibration=RoverCalibration.model_validate(TEST_CALIBRATION), capture_directory='',
                     nav_settings=NavSettings(pose_max_age_s=2.))  # stationary fixture, no phone pulse thread
    with TestClient(app) as client:
        def setup():
            state = app.state
            state.session = ('TEST', 1)
            state.db.execute('INSERT INTO sessions(session_id,map_epoch,created_at_ms) VALUES(?,?,?)', ('TEST', 1, 0))
            state.db.commit()
            state.phone = object()
            state.pose = SimpleNamespace(tracking='normal', t_capture=1., frame_id=1, transform=[1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.,0.,1.,1.,1.,1.])
            state.pose_at = state.detected_at = time.monotonic()
            grid = FakeOccupancy(np.ones((80, 80), np.uint8))
            state.occupancy = SimpleNamespace(session=state.session, map_snapshot=grid.snapshot)
            state.mode = 'explore'
            state.motion.begin()
            state.armed = state.auto_requested = True
        client.portal.call(setup)
        state = app.state
        yield client, state, car


class ExploreYieldTests(unittest.TestCase):
    def test_yield_holds_zero_then_replans_in_same_generation(self):
        with running_explore() as (client, state, car):
            generation = state.motion.generation
            response = client.post('/explore/yield', json=dict(generation=generation, reason='person_clearance_unknown'))
            self.assertEqual(response.status_code, 200)
            wait_for(lambda: state.nav.waiting_reason == 'person_clearance_unknown')
            self.assertTrue(state.armed)
            self.assertEqual(state.motion.generation, generation)
            self.assertIsNone(state.motion.desired)
            self.assertEqual(client.get('/autonomy').json()['explore_yield'], 'person_clearance_unknown')
            clock = [0.]
            original = state.obstacle_gate.decide
            state.obstacle_gate.decide = lambda observation, now: original(observation, clock[0])
            for frame, now, expected in [(1, 0., True), (2, .1, True), (3, .2, True),
                                          (3, 1.3, True), (4, 1.3, False)]:
                clock[0] = now
                state.detection_view = (dict(session_id='TEST', map_epoch=1, frame_id=frame,
                                            t_wall_ms=int(time.time()*1000), detections=[]), b'')
                response = client.post('/explore/resume', json=dict(generation=generation))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['yielding'], expected)
                self.assertEqual(response.json()['resumed'], not expected)
            wait_for(lambda: state.nav.waiting_reason != 'person_clearance_unknown')
            self.assertTrue(state.armed)
            self.assertEqual(state.motion.generation, generation)


    def test_unknown_depth_breaks_clear_hold_at_api_boundary(self):
        with running_explore() as (client, state, car):
            generation = state.motion.generation
            client.post('/explore/yield', json=dict(generation=generation, reason='person_path_crossing'))
            clock = [0.]
            original = state.obstacle_gate.decide
            state.obstacle_gate.decide = lambda observation, now: original(observation, clock[0])
            frames = [(1, 0., [], True), (2, .1, [], True), (3, .2, [], True),
                      (4, 1.3, [dict(**{'class': 'person'}, position=None)], True),
                      (5, 1.4, [], True), (6, 1.5, [], True), (7, 1.6, [], True),
                      (8, 2.7, [], False)]
            for frame, now, detections, expected in frames:
                clock[0] = now
                state.detection_view = (dict(session_id='TEST', map_epoch=1, frame_id=frame,
                                            t_wall_ms=int(time.time()*1000), detections=detections), b'')
                response = client.post('/explore/resume', json=dict(generation=generation))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['yielding'], expected, (frame, response.json()))
                self.assertTrue(state.armed)
            self.assertEqual(state.motion.generation, generation)

    def test_stop_fault_and_old_generation_cannot_be_released_or_rearmed(self):
        for fault in (False, True):
            with self.subTest(fault=fault), running_explore() as (client, state, car):
                generation = state.motion.generation
                self.assertEqual(client.post('/explore/yield', json=dict(generation=generation, reason='person_path_crossing')).status_code, 200)
                self.assertEqual(client.post('/explore/resume', json=dict(generation=generation-1)).status_code, 409)
                if fault:
                    state.pose.tracking = 'limited'
                    wait_for(lambda: not state.armed)
                else:
                    client.post('/stop')
                self.assertFalse(state.auto_requested)
                state.pose.tracking = 'normal'
                state.pose_at = state.detected_at = time.monotonic()
                self.assertEqual(client.post('/explore/resume', json=dict(generation=generation)).status_code, 409)
                time.sleep(.05)
                self.assertFalse(state.armed)

    def test_image_person_label_alone_does_not_emergency_latch(self):
        with running_explore() as (client, state, car):
            # A classifier can label a poster; this supplies no metric/path evidence.
            from backend.detections import detections_message
            from backend.localization import Detection
            result = SimpleNamespace(session_id='TEST', map_epoch=1, frame_id=1, t_capture=1.,
                                     t_wall_ms=int(time.time()*1000), image_size=(320, 240),
                                     boxes=[Detection(box=(10., 10., 50., 120.), class_name='person', confidence=.95)], found=[])
            view = detections_message(result, [], ('person',))
            self.assertIsNone(view['detections'][0]['position'])
            state.detection_view = (view, b'')
            time.sleep(.1)
            self.assertTrue(state.armed)
            self.assertIsNone(state.explore_yield)
            self.assertTrue(state.auto_requested)
