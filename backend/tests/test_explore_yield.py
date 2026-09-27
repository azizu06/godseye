"""Ordinary-obstruction API uses an existing fake drive session, never arm/prepare."""
from contextlib import contextmanager
from dataclasses import replace
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
            state.pose = SimpleNamespace(tracking='normal', transform=[1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.,0.,1.,1.,1.,1.])
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
            self.assertEqual(client.post('/explore/resume', json=dict(generation=generation)).status_code, 200)
            wait_for(lambda: state.nav.waiting_reason != 'person_clearance_unknown')
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
            state.detection_view = (dict(version=1, type='detections', boxes=[dict(class_name='person', confidence=.95)]), b'')
            time.sleep(.1)
            self.assertTrue(state.armed)
            self.assertIsNone(state.explore_yield)
            self.assertTrue(state.auto_requested)
