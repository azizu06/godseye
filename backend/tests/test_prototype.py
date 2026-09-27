"""Prototype bounds use synthetic dimensions, never hardware measurements."""
import math
import unittest
from pydantic import ValidationError
from backend.prototype import PrototypeActuation, prototype_geometry
from backend.calibration import RoverCalibration
from backend.rover_relay import RelayCar
from backend.motion import DriveCommand


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

    def test_fixed_power_no_reverse_arcs_or_rate_claims(self):
        profile = PrototypeActuation()
        for v, w, direction in [(.05, 0., 3), (0., .2, 1), (0., -.2, 2)]:
            command = profile.command(v, w)
            self.assertEqual((command.direction, command.pwm, command.lease_ms), (direction, 60, 200))
        self.assertIsNone(profile.command(0., 0.))
        self.assertIsNone(profile.stopping_distance_m)
        for v, w in [(-.01, 0.), (.21, 0.), (.01, .1), (0., .51), (math.nan, 0.)]:
            with self.assertRaises(ValueError): profile.command(v, w)

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
            self.assertEqual((packet['power'], packet['lease_ms']), (60, 200))
            self.assertEqual(car.armed_session, session.upper())
