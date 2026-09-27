"""Pure unit tests for the reactive clearance regulator; no grid, no I/O, no hardware."""
import unittest

from backend.reactive_clearance import (FRONT, FRONT_LEFT, FRONT_RIGHT, LEFT, RIGHT,
                                        ClearanceBounds, DirectionalRange, RangeState, regulate)

BOUNDS = ClearanceBounds()


def free(angle_deg, distance_m, stale=False):
    return DirectionalRange(angle_deg, distance_m, RangeState.FREE, stale=stale)


def occupied(angle_deg, distance_m=None, stale=False):
    return DirectionalRange(angle_deg, distance_m, RangeState.OCCUPIED, stale=stale)


def unknown(angle_deg):
    return DirectionalRange(angle_deg, None, RangeState.UNKNOWN)


def open_corridor():
    return [free(FRONT, 5.0), free(FRONT_LEFT, 5.0), free(FRONT_RIGHT, 5.0),
            free(LEFT, 5.0), free(RIGHT, 5.0)]


class OpenCorridorTests(unittest.TestCase):
    def test_all_clear_gives_full_speed_and_no_bias(self):
        envelope = regulate(open_corridor(), BOUNDS)
        self.assertEqual(envelope.speed_scale, 1.0)
        self.assertTrue(envelope.corridor_sufficient)
        self.assertIsNone(envelope.preferred_side)
        self.assertFalse(envelope.unresolved)


class ApproachingObstacleTests(unittest.TestCase):
    def test_speed_scale_drops_monotonically_as_front_range_shrinks(self):
        ranges = [free(FRONT_LEFT, 5.0), free(FRONT_RIGHT, 5.0)]
        scales = [regulate(ranges + [free(FRONT, d)], BOUNDS).speed_scale
                  for d in (5.0, 1.5, BOUNDS.slow_start_m, 0.5, BOUNDS.stop_scale_m, 0.1)]
        for earlier, later in zip(scales, scales[1:]):
            self.assertGreaterEqual(earlier, later)
        self.assertEqual(scales[0], 1.0)
        self.assertEqual(scales[-1], BOUNDS.min_speed_scale)

    def test_limiting_angle_names_the_front_ray(self):
        envelope = regulate([free(FRONT, 0.5), free(FRONT_LEFT, 5.0), free(FRONT_RIGHT, 5.0)], BOUNDS)
        self.assertEqual(envelope.limiting_angle_deg, FRONT)


class AsymmetricWallsTests(unittest.TestCase):
    def test_prefers_the_side_with_more_clearance(self):
        ranges = [free(FRONT, 2.0), free(FRONT_LEFT, 0.5), free(FRONT_RIGHT, 2.0),
                  free(LEFT, 0.4), free(RIGHT, 2.0)]
        envelope = regulate(ranges, BOUNDS)
        self.assertEqual(envelope.preferred_side, 'right')
        self.assertTrue(envelope.corridor_sufficient)
        self.assertLess(envelope.speed_scale, 1.0)

    def test_within_deadband_gives_no_preference(self):
        ranges = [free(FRONT, 2.0), free(FRONT_LEFT, 1.0), free(FRONT_RIGHT, 1.05),
                  free(LEFT, 1.0), free(RIGHT, 1.05)]
        envelope = regulate(ranges, BOUNDS)
        self.assertIsNone(envelope.preferred_side)


class InsufficientGapTests(unittest.TestCase):
    def test_close_occupied_front_marks_corridor_insufficient(self):
        ranges = [occupied(FRONT, 0.10), free(FRONT_LEFT, 2.0), free(FRONT_RIGHT, 2.0)]
        envelope = regulate(ranges, BOUNDS)
        self.assertFalse(envelope.corridor_sufficient)
        self.assertEqual(envelope.speed_scale, BOUNDS.min_speed_scale)

    def test_gap_at_threshold_is_sufficient_just_above_it_is_not(self):
        above = regulate([free(FRONT, BOUNDS.min_gap_m + 0.01), free(FRONT_LEFT, 2.0),
                          free(FRONT_RIGHT, 2.0)], BOUNDS)
        below = regulate([free(FRONT, BOUNDS.min_gap_m - 0.01), free(FRONT_LEFT, 2.0),
                          free(FRONT_RIGHT, 2.0)], BOUNDS)
        self.assertTrue(above.corridor_sufficient)
        self.assertFalse(below.corridor_sufficient)


class UnknownOrStaleRangeTests(unittest.TestCase):
    def test_unknown_front_is_never_treated_as_free(self):
        ranges = [unknown(FRONT), free(FRONT_LEFT, 5.0), free(FRONT_RIGHT, 5.0)]
        envelope = regulate(ranges, BOUNDS)
        self.assertTrue(envelope.unresolved)
        self.assertFalse(envelope.corridor_sufficient)

    def test_stale_reading_is_treated_as_unknown_even_if_labeled_free(self):
        stale_front = free(FRONT, 5.0, stale=True)
        fresh_envelope = regulate([free(FRONT, 5.0), free(FRONT_LEFT, 5.0), free(FRONT_RIGHT, 5.0)], BOUNDS)
        stale_envelope = regulate([stale_front, free(FRONT_LEFT, 5.0), free(FRONT_RIGHT, 5.0)], BOUNDS)
        self.assertFalse(fresh_envelope.unresolved)
        self.assertTrue(stale_envelope.unresolved)
        self.assertFalse(stale_envelope.corridor_sufficient)

    def test_missing_ray_is_unresolved_not_free(self):
        envelope = regulate([free(FRONT_LEFT, 5.0), free(FRONT_RIGHT, 5.0)], BOUNDS)
        self.assertTrue(envelope.unresolved)
        self.assertFalse(envelope.corridor_sufficient)

    def test_unresolved_side_ray_suppresses_side_preference(self):
        ranges = [free(FRONT, 2.0), free(FRONT_LEFT, 0.5), free(FRONT_RIGHT, 2.0),
                  unknown(LEFT), free(RIGHT, 2.0)]
        envelope = regulate(ranges, BOUNDS)
        self.assertIsNone(envelope.preferred_side)


class CornerTests(unittest.TestCase):
    def test_symmetric_close_diagonals_slow_down_without_side_bias(self):
        ranges = [free(FRONT, 2.0), free(FRONT_LEFT, 0.35), free(FRONT_RIGHT, 0.35)]
        envelope = regulate(ranges, BOUNDS)
        self.assertTrue(envelope.corridor_sufficient)
        self.assertLess(envelope.speed_scale, 1.0)
        self.assertIsNone(envelope.preferred_side)
        self.assertIn(envelope.limiting_angle_deg, (FRONT_LEFT, FRONT_RIGHT))


class ExistingRouteConstraintTests(unittest.TestCase):
    def test_route_active_suppresses_side_preference_despite_asymmetry(self):
        ranges = [free(FRONT, 2.0), free(FRONT_LEFT, 0.5), free(FRONT_RIGHT, 2.0),
                  free(LEFT, 0.4), free(RIGHT, 2.0)]
        without_route = regulate(ranges, BOUNDS, route_active=False)
        with_route = regulate(ranges, BOUNDS, route_active=True)
        self.assertEqual(without_route.preferred_side, 'right')
        self.assertIsNone(with_route.preferred_side)
        self.assertEqual(with_route.speed_scale, without_route.speed_scale)
        self.assertEqual(with_route.corridor_sufficient, without_route.corridor_sufficient)


class NeverIssuesMotionTests(unittest.TestCase):
    def test_output_has_no_motor_or_reverse_fields(self):
        envelope = regulate(open_corridor(), BOUNDS)
        fields = envelope.__dataclass_fields__.keys()
        for forbidden in ('throttle', 'pwm', 'steer', 'reverse', 'command'):
            self.assertNotIn(forbidden, fields)
        self.assertIn(envelope.preferred_side, (None, 'left', 'right'))


if __name__ == '__main__':
    unittest.main()
