"""Hardware-free proof. All rates/dimensions here are synthetic TEST values."""
import json
import math
import unittest

from pydantic import ValidationError

from backend.actuation import ActuationCalibration
from backend.navigation import PurePursuit
from backend.navigator import pose_from_transform
from backend.tests.test_navigation import camera_transform


def fixture(**changes):
    data = dict(
        version=1, measured_by='TEST fixture, not real rover data',
        forward=[dict(pwm=20, rate=.04), dict(pwm=60, rate=.16)],
        reverse=None,
        left=[dict(pwm=20, rate=.15), dict(pwm=65, rate=.45)],
        right=[dict(pwm=25, rate=.18), dict(pwm=70, rate=.48)],
        positive_yaw_direction='left', stopping_distance_m=.02, stop_time_ms=180.)
    data.update(changes)
    return ActuationCalibration.model_validate_json(json.dumps(data))


class ActuationTests(unittest.TestCase):
    def test_interpolates_only_inside_measured_range_and_keeps_timed_bound(self):
        model = fixture()
        command = model.command(.1, 0.)
        self.assertEqual((command.direction, command.pwm, command.lease_ms), (3, 40, 200))
        self.assertEqual(model.command(0., .3).direction, 1)
        self.assertEqual(model.command(0., -.3).direction, 2)
        self.assertIsNone(model.command(0., 0.))
        for v, w in [(.03, 0.), (.17, 0.), (-.1, 0.), (0., .1), (0., .49), (.1, .2),
                     (.21, 0.), (0., .51), (float('nan'), 0.), (0., float('inf'))]:
            with self.subTest(v=v, w=w), self.assertRaises(ValueError):
                model.command(v, w)

    def test_yaw_direction_comes_from_measurement(self):
        model = fixture(positive_yaw_direction='right')
        self.assertEqual(model.command(0., .3).direction, 2)
        self.assertEqual(model.command(0., -.3).direction, 1)

    def test_no_measurements_no_motion_but_stop_always_available(self):
        model = fixture(forward=None, measured_by=None, stop_time_ms=None)
        self.assertIn('actuation_forward_unmeasured', model.blockers)
        self.assertIn('actuation_measured_by_unmeasured', model.blockers)
        self.assertIn('actuation_stop_time_ms_unmeasured', model.blockers)
        self.assertIsNone(model.command(0., 0.))
        with self.assertRaises(ValueError): model.command(.1, 0.)
        with self.assertRaises(ValueError): model.follower()

    def test_rejects_bad_measurements_and_unusable_low_speed_range(self):
        for changes in [dict(forward=[]), dict(left=[dict(pwm=20, rate=.2)]),
                        dict(forward=[dict(pwm=50, rate=.1), dict(pwm=40, rate=.15)]),
                        dict(right=[dict(pwm=20, rate=.3), dict(pwm=40, rate=.2)]),
                        dict(measured_by=' '), dict(stop_time_ms=301.),
                        dict(left=[dict(pwm=True, rate=.2), dict(pwm=50, rate=.3)])]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError): fixture(**changes)
        self.assertIn('forward_low_speed_unmeasured', fixture(
            forward=[dict(pwm=20, rate=.2), dict(pwm=60, rate=.4)]).blockers)
        self.assertIn('turn_low_speed_unmeasured', fixture(
            left=[dict(pwm=20, rate=.6), dict(pwm=60, rate=.8)]).blockers)

    def test_pivot_follower_reaches_goal_using_only_encodable_commands(self):
        model = fixture()
        follower = PurePursuit([(0., 0.), (.7, .7), (1., 1.5)], model.follower())
        x = z = yaw = 0.
        turned = advanced = False
        for _ in range(600):
            command = follower.step(x, z, yaw)
            packet = model.command(command.v_mps, command.yaw_rate_rps)
            self.assertFalse(command.v_mps != 0 and command.yaw_rate_rps != 0)
            if command.arrived:
                self.assertIsNone(packet)
                break
            turned |= command.yaw_rate_rps != 0
            advanced |= command.v_mps != 0
            yaw += command.yaw_rate_rps * .1
            x += math.sin(yaw) * command.v_mps * .1
            z += math.cos(yaw) * command.v_mps * .1
        else:
            self.fail('calibrated pivot follower never arrived')
        self.assertTrue(turned and advanced)
        self.assertLessEqual(math.hypot(x - 1., z - 1.5), .15)


class MountHeadingTests(unittest.TestCase):
    def test_subtracts_measured_camera_to_chassis_yaw_and_preserves_v1_position(self):
        transform = camera_transform((math.sin(.8), math.cos(.8)))
        transform[12], transform[14] = 1.5, -2.
        x, z, yaw = pose_from_transform(transform, .3)
        self.assertEqual((x, z), (1.5, -2.))
        self.assertAlmostEqual(yaw, .5)
        self.assertAlmostEqual(pose_from_transform(transform)[2], .8)

    def test_mount_correction_wraps_at_pi(self):
        transform = camera_transform((math.sin(3.), math.cos(3.)))
        self.assertAlmostEqual(pose_from_transform(transform, -.5)[2], 3.5 - 2 * math.pi)


if __name__ == '__main__': unittest.main()
