"""Drive command safety against a fake car and a hand-stepped clock; no hardware."""
import unittest

from backend.drive import FakeCar
from backend.motion import Motion, MotionLimits


class Clock:
    def __init__(self):
        self.now = 100.

    def __call__(self):
        return self.now


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.car, self.clock, self.stops = FakeCar(), Clock(), []
        self.hazard = None
        self.motion = Motion(self.car, check=lambda command: self.hazard, stop=self.stop, clock=self.clock)

    def stop(self, reason):
        self.stops.append(reason)
        self.motion.halt()

    def sends(self):
        return [call for call in self.car.calls if call[0] == 'send']

    def arm(self):
        generation = self.motion.begin()
        self.car.calls.clear()  # arming sends its own zero (test_arming_again_...)
        return generation

    def test_armed_command_is_clamped_and_sent_once_per_tick(self):
        generation = self.arm()
        self.assertTrue(self.motion.submit(generation, 'manual', .1, .1))
        self.assertTrue(self.motion.submit(generation, 'manual', .2, -.9))  # newest wins
        self.motion.tick()
        self.assertEqual(self.car.calls, [('send', .15, -.5)])

    def test_unrenewed_command_expires_to_one_zero(self):
        generation = self.arm()
        for _ in range(3):  # a held button renewed every 100 ms keeps moving
            self.motion.submit(generation, 'manual', .1, 0.)
            self.motion.tick()
            self.clock.now += .1
        self.clock.now += .2  # renewals stop: 300 ms since the last one
        for _ in range(3):
            self.motion.tick()
        self.assertEqual(self.car.calls, [('send', .1, 0.)] * 3 + [('zero',)])
        self.assertEqual(self.stops, [])  # expiry zeroes but leaves the operator armed

    def test_halt_zeroes_and_a_new_arm_never_revives_older_commands(self):
        old = self.arm()
        self.motion.submit(old, 'navigate', .1, .2)
        self.motion.halt()
        for _ in range(3):
            self.motion.tick()
        self.assertEqual(self.car.calls, [('zero',)])
        self.assertFalse(self.motion.submit(old, 'navigate', .1, .2))  # disarmed
        new = self.motion.begin()
        self.motion.tick()
        self.assertFalse(self.motion.submit(old, 'navigate', .1, .2))  # a goal from before the stop
        self.motion.tick()
        self.assertEqual(self.car.calls, [('zero',), ('zero',)])
        self.assertTrue(self.motion.submit(new, 'manual', .1, 0.))
        self.motion.tick()
        self.assertEqual(self.sends(), [('send', .1, 0.)])

    def test_arming_again_drops_a_held_command_with_a_zero(self):
        old = self.motion.begin()
        self.motion.submit(old, 'manual', .1, 0.)
        self.motion.tick()
        new = self.motion.begin()  # a latched car must not keep the old command
        self.motion.tick()
        self.assertNotEqual(old, new)
        self.assertEqual(self.car.calls, [('zero',), ('send', .1, 0.), ('zero',)])

    def test_non_finite_command_is_refused_and_zeroes_the_held_one(self):
        generation = self.arm()
        self.motion.submit(generation, 'navigate', .1, 0.)
        for bad in [(float('nan'), 0.), (.1, float('inf'))]:
            with self.assertRaises(ValueError):
                self.motion.submit(generation, 'navigate', *bad)
        self.motion.tick()
        self.assertEqual(self.car.calls, [('zero',), ('zero',)])

    def test_limits_cannot_exceed_the_contract_maximums(self):
        self.assertEqual(MotionLimits(max_speed_mps=.2).max_speed_mps, .2)
        for bad in [dict(max_speed_mps=.21), dict(max_speed_mps=float('nan')), dict(max_speed_mps=0.),
                    dict(max_yaw_rate_rps=.6), dict(lease_s=.3), dict(lease_s=0.)]:
            with self.assertRaises(ValueError):
                MotionLimits(**bad)

    def test_hazard_at_dispatch_stops_instead_of_sending(self):
        generation = self.arm()
        self.motion.submit(generation, 'manual', .1, 0.)
        self.hazard = 'car_stale'
        self.motion.tick()
        self.motion.tick()
        self.assertEqual((self.stops, self.car.calls), (['car_stale'], [('zero',)]))

    def test_adapter_send_error_fails_closed(self):
        generation = self.arm()
        self.motion.submit(generation, 'manual', .1, 0.)
        self.car.error = OSError('link dropped')
        with self.assertLogs('backend.motion', 'ERROR'):
            self.motion.tick()
        self.car.error = None
        self.motion.tick()
        self.assertEqual((self.stops, self.car.calls), (['car_error'], [('zero',)]))
        self.assertFalse(self.motion.active)

    def test_a_failed_zero_is_retried_every_tick_and_blocks_arming(self):
        generation = self.arm()
        self.motion.submit(generation, 'manual', .1, 0.)
        self.car.error = OSError('link dropped')  # a dropped link fails zero too
        with self.assertLogs('backend.motion', 'ERROR') as logs:
            self.motion.halt()
            for _ in range(3):
                self.motion.tick()
            self.assertIsNone(self.motion.begin())  # no arm without a confirmed zero
        self.assertEqual(len(logs.output), 1)  # retries stay quiet
        self.assertFalse(self.motion.submit(generation, 'manual', .1, 0.))
        self.car.error = None
        self.motion.tick()
        self.motion.tick()
        self.assertEqual(self.car.calls, [('zero',)])  # retried until it landed, then stopped retrying
        self.assertIsNotNone(self.motion.begin())

    def test_a_failed_expiry_zero_latches_a_car_error_stop(self):
        generation = self.arm()
        self.motion.submit(generation, 'manual', .1, 0.)
        self.clock.now += .3
        self.car.error = OSError('link dropped')
        with self.assertLogs('backend.motion', 'ERROR'):
            self.motion.tick()
        self.assertEqual(self.stops, ['car_error'])
        self.assertFalse(self.motion.active)

if __name__ == '__main__':
    unittest.main()
