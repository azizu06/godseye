"""Synthetic benchmark mechanics, never evidence of hardware performance."""
import json
import math
from pathlib import Path
import tempfile
import unittest

from backend.actuation import TimedMotorCommand
from backend.navigation import Grid
from tools.scout_benchmark import (
    Footprint, Pose, Scenario, SyntheticResponse, collides, integrate,
    make_scenarios, run_scenario, run_suite, save_report,
)


class BenchmarkGeometryTests(unittest.TestCase):
    def test_rotated_chassis_hits_a_wall_even_when_its_center_is_free(self):
        grid = Grid.from_array([[1, 1, 2], [1, 1, 2], [1, 1, 2]],
                               origin=(0., 0.), cell_m=1.)
        body = Footprint(length_m=1.4, width_m=.4, margin_m=.05)
        self.assertFalse(collides(grid, Pose(1.5, 1.5, 0.), body))
        self.assertTrue(collides(grid, Pose(1.5, 1.5, math.pi / 2), body))
        self.assertTrue(collides(grid, Pose(.1, 1.5, 0.), body))

    def test_exact_arc_integration_uses_backend_positive_yaw_sign(self):
        pose = integrate(Pose(0., 0., 0.), .2, .5, 2.)
        self.assertAlmostEqual(pose.x, .4 * (1 - math.cos(1.)))
        self.assertAlmostEqual(pose.z, .4 * math.sin(1.))
        self.assertAlmostEqual(pose.yaw, 1.)
        straight = integrate(Pose(0., 0., math.pi / 2), .2, 0., 2.)
        self.assertAlmostEqual(straight.x, .4)
        self.assertAlmostEqual(straight.z, 0.)

    def test_contact_on_a_cell_edge_counts_as_collision(self):
        grid = Grid.from_array([[2, 1, 1, 1]] * 4, origin=(0., 0.), cell_m=1.)
        body = Footprint(length_m=.4, width_m=.4)
        self.assertTrue(collides(grid, Pose(1.2, 2., 0.), body))
        self.assertFalse(collides(grid, Pose(1.20001, 2., 0.), body))

    def test_packets_produce_distinct_pivot_straight_and_half_full_arcs(self):
        response = SyntheticResponse(wheel_speed_mps=.36, track_width_m=.3)
        expected = [(1, 0., 2.4), (2, 0., -2.4), (3, .36, 0.),
                    (5, .27, .6), (6, .27, -.6)]
        for direction, speed, yaw in expected:
            with self.subTest(direction=direction):
                v, w = response.rates(TimedMotorCommand(direction, 180))
                self.assertAlmostEqual(v, speed)
                self.assertAlmostEqual(w, yaw)
        self.assertEqual(response.rates(None), (0., 0.))
        self.assertAlmostEqual(response.rates(TimedMotorCommand(3, 60))[0], .12)

    def test_nonsensical_response_and_footprint_are_rejected(self):
        for kwargs in ({'track_width_m': 0.}, {'wheel_speed_mps': -1.},
                       {'wheel_speed_mps': math.nan}):
            with self.assertRaises(ValueError):
                SyntheticResponse(**kwargs)
        with self.assertRaises(ValueError):
            Footprint(length_m=0., width_m=.2)


class BenchmarkRunTests(unittest.TestCase):
    def test_close_goal_finishes_with_terminal_zero_and_measured_travel(self):
        scenario = next(s for s in make_scenarios() if s.name == 'goal_approach')
        result = run_scenario(scenario)
        self.assertTrue(result['metrics']['completed'], result['metrics'])
        self.assertFalse(result['metrics']['collision'])
        self.assertLessEqual(result['metrics']['final_goal_distance_m'], .15)
        trace = result['trace']
        self.assertGreater(len(trace), 10)
        self.assertEqual(trace[-1]['packet'], None)
        self.assertEqual(trace[-1]['requested'], [0., 0.])
        distance = sum(math.hypot(b['x'] - a['x'], b['z'] - a['z'])
                       for a, b in zip(trace, trace[1:]))
        self.assertAlmostEqual(result['metrics']['distance_m'], distance, places=5)
        self.assertAlmostEqual(result['metrics']['elapsed_s'], trace[-1]['t'])

    def test_stalled_drive_is_reported_instead_of_passing_because_a_path_exists(self):
        scenario = next(s for s in make_scenarios() if s.name == 'goal_approach')
        result = run_scenario(scenario, response=SyntheticResponse(wheel_speed_mps=0.))
        self.assertTrue(result['plans'][0]['points'])
        self.assertFalse(result['metrics']['completed'])
        self.assertEqual(result['metrics']['stop_reason'], 'no_progress')
        self.assertEqual(result['metrics']['distance_m'], 0.)
        self.assertAlmostEqual(result['metrics']['elapsed_s'], 5.)

    def test_collision_at_start_is_reported_before_motion(self):
        grid = Grid.from_array([[2] * 20] * 20, origin=(0., 0.), cell_m=.1)
        scenario = Scenario('blocked', 'Blocked start', grid, Pose(.5, .5, 0.), (.8, .8))
        result = run_scenario(scenario)
        self.assertEqual(result['metrics']['stop_reason'], 'collision')
        self.assertTrue(result['metrics']['collision'])
        self.assertEqual(result['metrics']['elapsed_s'], 0.)

    def test_suite_is_deterministic_and_reports_all_four_scenarios(self):
        first = run_suite()
        self.assertEqual(first, run_suite())
        self.assertEqual({r['name'] for r in first['scenarios']},
                         {'off_center_corridor', 'obstacle_detour', 'right_angle_corner', 'goal_approach'})
        for result in first['scenarios']:
            with self.subTest(scenario=result['name']):
                self.assertTrue(result['metrics']['completed'], result['metrics'])
                self.assertFalse(result['metrics']['collision'])
                self.assertTrue(result['plans'][0]['points'], result['metrics'])
                self.assertGreater(result['metrics']['elapsed_s'], 0.)
                self.assertGreater(result['metrics']['distance_m'], 0.)
                directions = [p['packet'][0] if p['packet'] else 0 for p in result['trace'][:-1]]
                steering = [{1: 1, 2: -1, 5: 1, 6: -1}.get(d, 0) for d in directions]
                switches = sum(a != b for a, b in zip(steering, steering[1:]))
                self.assertEqual(result['metrics']['steering_switches'], switches)

    def test_report_serializes_trace_and_embeds_same_data_without_remote_assets(self):
        report = run_suite(names=['goal_approach'])
        with tempfile.TemporaryDirectory() as temp:
            paths = save_report(report, Path(temp))
            self.assertEqual(json.loads(paths[0].read_text()), report)
            html = paths[1].read_text()
            data = html.split('<script id="benchmark-data" type="application/json">', 1)[1].split('</script>', 1)[0]
            self.assertEqual(json.loads(data), report)
            self.assertNotIn('<script src=', html)
            self.assertNotIn('<link ', html)


if __name__ == '__main__':
    unittest.main()
