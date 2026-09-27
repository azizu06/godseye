"""Opt-in faster prototype cruise: PWM policy, proximity slowdown and wiring only.

No physical actuation. Power values are uncalibrated duty, never measured speed.
"""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from backend.actuation import TimedMotorCommand
from backend.motion import DriveCommand
from backend.navigator import exploration_command, obstacle_clearance_m
from backend.prototype import PrototypeActuation, prototype_geometry
from backend.rover_relay import RelayCar
from backend.tests.test_navigator import FakeOccupancy, Harness, Rover, cells, initial_plan

SPEEDS = (0., .04, .05, .08, .1, .11, .12, .13, .15, .17, .2)
YAWS = (-.5, -.3, -.15, -.1, 0., .1, .15, .3, .5)


def legacy(v, w, max_pwm=180, variable=False):
    """The pre-option mapping, kept verbatim here to prove the default is unchanged."""
    if v == 0 and w == 0:
        return None
    if not variable:
        power = min(max_pwm, round(60 + 120 * max(0., (v - .15) / .05)))
        if v and abs(w) >= .15:
            return TimedMotorCommand(5 if w > 0 else 6, power)
        return TimedMotorCommand(3 if v else (1 if w > 0 else 2), power)
    if not v:
        return TimedMotorCommand(1 if w > 0 else 2, min(60, max_pwm))
    power = min(max_pwm, round(60 + 120 * max(0., min(1., (v - .05) / .15))))
    inner = round(power * (1. - abs(w)))
    if inner == power:
        return TimedMotorCommand(3, power)
    return TimedMotorCommand(5 if w > 0 else 6, power, inner_power=max(power // 2, inner))


class CruisePolicyTests(unittest.TestCase):
    def test_option_is_bounded_and_rejects_nonsense(self):
        for cruise, ceiling in ((60, 180), (0, 180), (-5, 180), (181, 180), (255, 180),
                                (121, 120), (120.5, 180), (True, 180), ('140', 180)):
            with self.subTest(cruise=cruise, ceiling=ceiling), self.assertRaises(ValueError):
                PrototypeActuation(max_pwm=ceiling, cruise_pwm=cruise)
        for cruise in (61, 120, 180):
            self.assertEqual(PrototypeActuation(cruise_pwm=cruise).cruise_pwm, cruise)

    def test_default_commands_and_warnings_are_unchanged(self):
        for ceiling in (60, 120, 180):
            for variable in (False, True):
                profile = PrototypeActuation(max_pwm=ceiling, variable_arc_pwm=variable)
                self.assertIsNone(profile.cruise_pwm)
                self.assertIs(profile.warnings, PrototypeActuation.warnings)
                self.assertEqual(profile.follower(), PrototypeActuation(variable_arc_pwm=variable).follower())
                for v in SPEEDS:
                    for w in YAWS:
                        with self.subTest(ceiling=ceiling, variable=variable, v=v, w=w):
                            self.assertEqual(profile.command(v, w), legacy(v, w, ceiling, variable))
        self.assertEqual(len(PrototypeActuation.warnings), 3)

    def test_faster_cruise_raises_forward_and_arc_power_only(self):
        for variable in (False, True):
            default = PrototypeActuation(variable_arc_pwm=variable)
            faster = PrototypeActuation(variable_arc_pwm=variable, cruise_pwm=150)
            self.assertEqual(faster.follower(), default.follower())  # same nominal kinematics
            for v in SPEEDS:
                for w in YAWS:
                    with self.subTest(variable=variable, v=v, w=w):
                        base, fast = default.command(v, w), faster.command(v, w)
                        if base is None:
                            self.assertIsNone(fast)
                            continue
                        self.assertEqual(fast.direction, base.direction)
                        self.assertLessEqual(fast.pwm, 180)
                        self.assertGreaterEqual(fast.pwm, base.pwm)  # never below default
                        if v == 0 or v <= .1:
                            self.assertEqual(fast, base)  # pivots and slow requests unchanged
                        if v >= .15:
                            self.assertGreaterEqual(fast.pwm, 150)
        fixed_default, fixed_fast = PrototypeActuation(), PrototypeActuation(cruise_pwm=150)
        # Nominal cruise, straight and arc, was PWM 60 in the checkpoint-5 command format.
        self.assertEqual(fixed_default.command(.15, 0.).pwm, 60)
        self.assertEqual(fixed_fast.command(.15, 0.), TimedMotorCommand(3, 150))
        self.assertEqual(fixed_fast.command(.15, .3), TimedMotorCommand(5, 150))
        self.assertEqual(fixed_fast.command(.15, -.3), TimedMotorCommand(6, 150))
        self.assertEqual(fixed_fast.command(.125, 0.).pwm, 105)  # halfway up the boost band
        self.assertEqual(fixed_fast.command(0., .5), fixed_default.command(0., .5))

    def test_faster_cruise_relay_packets_keep_ceiling_and_lease(self):
        for variable in (False, True):
            profile = PrototypeActuation(max_pwm=170, variable_arc_pwm=variable, cruise_pwm=170)
            car = RelayCar('TEST_KEY_NOT_REAL_01234567890123456789', profile, clock=lambda: 10.)
            car.identity = lambda: ('capture', 1)
            car.attach()
            car.control = None  # fake completed Stop handshake, before injecting an armed session
            session = 'b' * 32
            car.armed_session = session.upper()
            seq = 0
            for v in SPEEDS:
                for w in YAWS:
                    seq += 1
                    car.receive(dict(version=1, type='status', seq=seq, session_id='capture', map_epoch=1,
                                     permit=f'{seq:016X}', uno_age_ms=100., enabled=True))
                    car.send(DriveCommand(session_id=session, seq=seq, v_mps=v, yaw_rate_rps=w,
                                          issued_at_ms=10000, valid_for_ms=250))
                    packet = car.next_message()
                    self.assertLessEqual(packet['power'], 170)
                    self.assertLessEqual(packet.get('inner_power', 0), 170)
                    self.assertEqual(packet['lease_ms'], 1500)
                    if not variable:
                        self.assertNotIn('inner_power', packet)

    def test_existing_proximity_cap_returns_faster_mode_to_baseline_power(self):
        base = FakeOccupancy(np.ones((100, 100), np.uint8)).snapshot()
        # Typical live evidence: a quarter-second-old map with medium depth confidence.
        snapshot = replace(base, sensing_confidence=.75)
        now = snapshot.accepted_at + .25
        default, faster = PrototypeActuation(), PrototypeActuation(cruise_pwm=150)
        open_v, open_w = exploration_command(snapshot, 2., 2., 0., .2, 0., now, 1.)
        obstacle = snapshot.cells.copy(); obstacle[42:44, 40:42] = 2  # 0.1 m ahead, inside inflation
        near = replace(snapshot, cells=obstacle)
        self.assertLess(obstacle_clearance_m(near, 2., 2.), near.inflation_m)
        near_v, near_w = exploration_command(near, 2., 2., 0., .2, 0., now, 1.)
        self.assertAlmostEqual(near_v, .1)
        self.assertGreater(faster.command(open_v, open_w).pwm, default.command(open_v, open_w).pwm)
        self.assertEqual(faster.command(open_v, open_w).pwm, 150)
        self.assertEqual(faster.command(near_v, near_w), default.command(near_v, near_w))
        self.assertEqual(faster.command(near_v, near_w).pwm, 60)
        unknown = snapshot.cells.copy(); unknown[44:60, 40] = 0
        unseen_v, _ = exploration_command(replace(snapshot, cells=unknown), 2., 2., 0., .2, 0., now, 1.)
        self.assertLess(faster.command(unseen_v, 0.).pwm, 150)


class GoalProximityTests(unittest.IsolatedAsyncioTestCase):
    async def run_goal(self, **settings):
        grid = cells()
        grid[:28, 29:31] = grid[38:, 29:31] = 2  # wall at x 1.45-1.55 m, 0.5 m gap at z 1.4-1.9 m
        occupancy = FakeOccupancy(grid)
        start, goal = (.5, 1.65), (2.5, 1.65)
        rover = Rover(*start, math.pi / 2)
        h = Harness(rover, occupancy, **settings)
        h.nav.start_goal(goal, initial_plan(occupancy.cells, start, goal), h.generation)
        await h.finished()
        self.assertEqual(h.stops, ['arrived'])
        snapshot = occupancy.snapshot()
        poses = [start] + rover.trace[:-1]
        return [(v, obstacle_clearance_m(snapshot, x, z, search_m=1.) - snapshot.inflation_m)
                for (v, _), (x, z) in zip(rover.commands, poses) if v > 0]

    async def test_faster_goal_runs_slow_beside_obstacles_but_default_goal_runs_do_not(self):
        default = await self.run_goal(adaptive_explore=True)
        slowed = await self.run_goal(adaptive_explore=True, proximity_slowdown=True)
        self.assertTrue(any(v >= .15 and room < .1 for v, room in default),
                        'default prototype goal runs keep nominal cruise through the gap')
        near = [(v, room) for v, room in slowed if room < .1]
        self.assertTrue(near)
        self.assertTrue(all(v <= .1 + .1 * max(0., room) / .4 + 1e-9 for v, room in near))
        self.assertTrue(any(v >= .15 for v, _ in slowed), 'open floor still reaches nominal cruise')
        faster = PrototypeActuation(cruise_pwm=150)
        self.assertTrue(all(faster.command(v, 0.).pwm <= 105 for v, _ in near))  # cap <= .125 nominal


class CruiseWiringTests(unittest.TestCase):
    def test_autonomy_reports_faster_mode_and_goal_slowdown(self):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        for cruise in (None, 140):
            with self.subTest(cruise=cruise):
                car = RelayCar('TEST_KEY_NOT_REAL_01234567890123456789', PrototypeActuation(cruise_pwm=cruise))
                app = create_app(db_path=':memory:', car=car, calibration=prototype_geometry(.24, .14),
                                 capture_directory='')
                with TestClient(app) as client:
                    status = client.get('/autonomy').json()
                self.assertEqual(status['profile'], 'prototype')
                self.assertEqual(status['prototype_cruise_pwm'], cruise)
                self.assertEqual(app.state.nav.settings.proximity_slowdown, cruise is not None)
                self.assertTrue(app.state.nav.settings.adaptive_explore)
                faster = [text for text in status['warnings'] if 'Faster uncalibrated cruise' in text]
                if cruise is None:
                    self.assertEqual(status['warnings'], list(PrototypeActuation.warnings))
                else:
                    self.assertEqual(len(faster), 1)
                    self.assertIn('PWM up to 140', faster[0])
                    self.assertIn('unmeasured', faster[0])

    def launch(self, *extra):
        from tools.run_rover_backend import main
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'pairing-key').write_text('TEST_KEY_NOT_REAL_01234567890123456789')
            args = ['run_rover_backend', '--config-dir', directory, *extra]
            output = io.StringIO()
            with patch('sys.argv', args), patch('backend.app.create_app') as create, \
                    patch('uvicorn.run') as run, redirect_stdout(output), redirect_stderr(io.StringIO()):
                main()
                run.assert_called_once()  # mocked: no port, backend, or real adapter was started
                return create.call_args.kwargs['car'].actuation, output.getvalue()

    def test_cli_flag_is_opt_in_and_bounded(self):
        prototype = ['--prototype', '--estimated-length-m', '.24', '--estimated-width-m', '.14']
        profile, output = self.launch(*prototype)
        self.assertIsNone(profile.cruise_pwm)
        self.assertNotIn('FASTER', output)
        profile, output = self.launch(*prototype, '--prototype-cruise-pwm', '120')
        self.assertEqual((profile.cruise_pwm, profile.max_pwm), (120, 180))
        self.assertEqual(profile.command(.15, 0.).pwm, 120)
        self.assertIn('FASTER UNCALIBRATED CRUISE', output)
        for bad in (['--prototype-cruise-pwm', '60'], ['--prototype-cruise-pwm', '181'],
                    ['--prototype-cruise-pwm', '255'], ['--prototype-cruise-pwm', 'fast'],
                    ['--prototype-cruise-pwm', '130', '--prototype-max-pwm', '120']):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.launch(*prototype, *bad)
        with self.assertRaises(SystemExit):
            self.launch('--prototype-cruise-pwm', '120')  # never implied without --prototype


if __name__ == '__main__':
    unittest.main()
