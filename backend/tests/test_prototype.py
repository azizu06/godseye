"""Prototype bounds use synthetic dimensions, never hardware measurements."""
import math
import time
import unittest
from types import SimpleNamespace
import numpy as np
from pydantic import ValidationError
from backend.prototype import PrototypeActuation, prototype_geometry
from backend.calibration import RoverCalibration
from backend.rover_relay import RelayCar
from backend.motion import DriveCommand
from backend.navigation import PurePursuit


class PrototypeTests(unittest.TestCase):
    def test_estimates_are_explicit_and_not_mislabeled_as_measurements(self):
        geometry = prototype_geometry(.24, .14)
        self.assertIsNone(geometry.measured_by)
        self.assertEqual(geometry.basis, 'operator_estimate')
        self.assertEqual(geometry.blockers, ())
        self.assertAlmostEqual(geometry.inflation_m, math.hypot(.24, .14) + .1524)
        # Normal calibration still blocks without evidence; prototype cannot be
        # loaded as a measured file, because its basis is an extra field.
        with self.assertRaises(ValidationError):
            RoverCalibration.model_validate(geometry.model_dump())
        normal = RoverCalibration.model_validate(geometry.model_dump(exclude={'basis'}))
        self.assertIn('calibration_unverified', normal.blockers)
        for length, width in [(0., .1), (.2, -1.), (math.nan, .1), (1.1, .1)]:
            with self.assertRaises(ValidationError): prototype_geometry(length, width)

    def test_api_labels_prototype_and_keeps_live_readiness_gates(self):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        car = RelayCar('TEST_KEY_NOT_REAL_01234567890123456789', PrototypeActuation())
        app = create_app(db_path=':memory:', car=car, calibration=prototype_geometry(.24, .14), capture_directory='')
        with TestClient(app) as client:
            self.assertEqual(app.state.motion.limits.max_speed_mps, .2)
            self.assertEqual(app.state.nav.settings.replan_s, 4.)
            self.assertEqual(app.state.nav.settings.pose_max_age_s, 1.)
            status = client.get('/autonomy').json()
            self.assertEqual(status['profile'], 'prototype')
            self.assertTrue(status['warnings'])
            self.assertFalse(status['ready'])
            self.assertFalse(status['armed'])
            self.assertIn('rover_relay_disconnected', status['blockers'])
            self.assertNotIn('calibration_unverified', status['blockers'])
            self.assertEqual(client.post('/arm').status_code, 401)
            response = client.post('/arm', headers={'Authorization': 'Bearer ' + car.key})
            self.assertEqual(response.status_code, 409)

    def test_explicit_stop_clears_latched_explore_request(self):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        car = RelayCar('TEST_KEY_NOT_REAL_01234567890123456789', PrototypeActuation())
        app = create_app(db_path=':memory:', car=car, calibration=prototype_geometry(.24, .14), capture_directory='')
        with TestClient(app) as client:
            client.app.state.auto_requested = True
            client.app.state.mode = 'explore'
            self.assertTrue(client.get('/autonomy').json()['auto_requested'])
            self.assertEqual(client.post('/stop').status_code, 200)
            self.assertFalse(client.get('/autonomy').json()['auto_requested'])

    def test_latched_explore_rearms_after_a_recoverable_disconnect(self):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        from backend.occupancy import OccupancySnapshot
        from backend.tests.test_objects import FakeDetector
        from backend.tests.test_map_transport import hello
        from tools.car_rehearsal import wait_for
        car = RelayCar('TEST_KEY_NOT_REAL_01234567890123456789', PrototypeActuation())
        car.connected = True
        car.health = lambda: 'ok'
        car.blockers = lambda: []
        async def prepare(session):
            car.armed_session = session.upper()
        car.prepare = prepare
        app = create_app(db_path=':memory:', car=car, detector=FakeDetector(),
                         calibration=prototype_geometry(.24, .14), capture_directory='')
        with TestClient(app) as client, client.websocket_connect('/phone') as phone:
            state = client.app.state
            phone.send_json(hello(session='test'))
            wait_for(lambda: state.session == ('test', 1))
            snapshot = OccupancySnapshot(('test', 1), 1, time.monotonic(), (), .18,
                                         (-1., -1.), .05, np.ones((40, 40), dtype=np.uint8), 0.)
            state.occupancy = SimpleNamespace(session=state.session, map_snapshot=lambda: snapshot)
            state.autonomy_map = snapshot
            def pose(frame_id, tracking='normal', delay_ms=0):
                return dict(version=1, type='pose', session_id='test', map_epoch=1,
                    frame_id=frame_id, t_capture=float(frame_id),
                    t_wall_ms=int(time.time()*1000) - delay_ms, tracking=tracking,
                    transform=[1.,0.,0.,0., 0.,1.,0.,0., 0.,0.,1.,0., 0.,0.,0.,1.])
            phone.send_json(pose(1))
            wait_for(lambda: state.pose is not None)
            state.detected_at = time.monotonic()
            state.mode = 'explore'
            state.auto_requested = True  # a previously armed Explore survived relay loss
            self.assertEqual(client.get('/autonomy').json()['blockers'], [])
            wait_for(lambda: state.armed, timeout=1.)
            self.assertTrue(client.get('/autonomy').json()['auto_requested'])
            phone.send_json(pose(2, delay_ms=1000))
            time.sleep(.05)
            self.assertTrue(state.armed, 'An old pose must not disarm persistent Explore')
            phone.send_json(pose(3, tracking='limited'))
            wait_for(lambda: state.pose.tracking == 'limited')
            self.assertTrue(state.armed, 'Tracking loss pauses the motor without losing Explore')
            phone.send_json(pose(4))
            wait_for(lambda: state.pose.tracking == 'normal')
            self.assertEqual(client.post('/stop').status_code, 200)
            self.assertFalse(state.auto_requested)

    def test_fixed_power_forward_arcs_and_no_reverse_or_rate_claims(self):
        profile = PrototypeActuation()
        for v, w, direction in [(.05, 0., 3), (0., .2, 1), (0., -.2, 2)]:
            command = profile.command(v, w)
            self.assertEqual((command.direction, command.pwm, command.lease_ms), (direction, 60, 1500))
        self.assertEqual((profile.command(.15, .3).direction, profile.command(.15, .3).pwm), (5, 180))
        self.assertEqual((profile.command(.15, -.3).direction, profile.command(.15, -.3).pwm), (6, 180))
        self.assertIsNone(profile.command(0., 0.))
        self.assertIsNone(profile.stopping_distance_m)
        for v, w in [(-.01, 0.), (.21, 0.), (0., .51), (math.nan, 0.)]:
            with self.assertRaises(ValueError): profile.command(v, w)

    def test_open_straight_cruise_has_more_power_than_slow_approach_or_pivot(self):
        profile = PrototypeActuation()
        self.assertEqual(profile.command(.05, 0.).pwm, 60)
        self.assertEqual(profile.command(0., .5).pwm, 60)
        self.assertEqual(profile.command(.15, 0.).pwm, 60)
        self.assertEqual(profile.command(.2, 0.).pwm, 180)

    def test_small_heading_noise_follows_with_a_moving_turn(self):
        path = [(0., 0.), (0., 3.)]
        profile = PrototypeActuation().follower()
        for yaw in (-.2, .2):
            self.assertEqual(PurePursuit(path, profile).step(0., .5, yaw).status, 'follow')
        turn = PurePursuit(path, profile).step(0., .5, .5)
        self.assertEqual(turn.status, 'follow')
        self.assertGreater(turn.v_mps, 0.)
        self.assertNotEqual(turn.yaw_rate_rps, 0.)

    def test_continuous_commands_have_no_added_pause_or_run_limit(self):
        now = [10.]
        car = RelayCar('TEST_KEY_NOT_REAL_01234567890123456789', PrototypeActuation(), clock=lambda: now[0])
        car.identity = lambda: ('capture', 1)
        car.attach()
        car.control = None
        session = 'a' * 32
        car.armed_session = session.upper()
        for seq, elapsed in enumerate([0., .05, .1, .15, 15., 30., 60.], 1):
            now[0] = 10. + elapsed
            car.receive(dict(version=1, type='status', seq=seq, session_id='capture', map_epoch=1,
                             permit=f'{seq:016X}', uno_age_ms=100., enabled=True))
            car.send(DriveCommand(session_id=session, seq=seq, v_mps=.15, yaw_rate_rps=0.,
                                  issued_at_ms=round(now[0]*1000), valid_for_ms=250))
            packet = car.next_message()
            self.assertEqual((packet['power'], packet['lease_ms']), (60, 1500))
            self.assertEqual(car.armed_session, session.upper())
