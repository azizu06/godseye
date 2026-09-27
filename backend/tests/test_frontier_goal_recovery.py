"""Explore04 frontier invalidation: real map/planner and generation-safe runner.

The live log's last 1Hz occupancy precedes the failing internal plan. The single
changed cell is an explicit counterfactual, not a reconstruction of absent depth.
"""
import base64
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import numpy as np

from backend.navigation import PlanResult, path_blocked, plan_path
from backend.navigator import Navigator, NavSettings, planning_grid
from backend.occupancy import OccupancySnapshot
from backend.prototype import PrototypeActuation, prototype_geometry
from backend.tests.test_explore_recovery import PolicyOccupancy, corridor
from backend.tests.test_navigator import Harness, Rover, wait_until


def recorded_case():
    record = json.loads(Path(__file__).with_name('fixtures').joinpath('recorded_explore_frontier.json').read_text())
    msg = record['occupancy']
    cells = np.frombuffer(base64.b64decode(msg['cells']), np.uint8).reshape(msg['height'], msg['width']).copy()
    change = record['counterfactual']
    assert cells[change['row'], change['col']] == change['original_value']
    cells[change['row'], change['col']] = change['value']
    snapshot = OccupancySnapshot(('recorded', 6), 1, time.monotonic(), (),
        prototype_geometry(.2413, .127).inflation_m, tuple(msg['origin']), msg['cell_m'], cells, msg['floor_y'], True)
    return record, snapshot


class RecordedFrontierTests(unittest.TestCase):
    def test_point_clear_frontier_with_blocked_route_cell_is_reselected(self):
        record, snapshot = recorded_case()
        settings = NavSettings(start_recovery_margin_m=.1524, follower=PrototypeActuation().follower())
        nav = Navigator(settings, pose=lambda: None, occupancy=lambda: snapshot,
                        submit=lambda *args: None, stop=lambda *args: None,
                        publish=lambda *args: None, armed_mode=lambda: 'explore')
        x, _, z = record['pose']['position']
        goal = tuple(record['goal'])
        grid, config = planning_grid(snapshot, settings)
        self.assertTrue(snapshot.traversable(*goal))
        self.assertEqual(plan_path(grid, (x, z), goal, config).reason, 'goal_occupied')
        outcome = nav._plan(lambda: snapshot, (x, z), goal, True,
                            yaw=record['pose']['yaw_rad'], explore_yaw=record['explore_heading'])
        self.assertEqual(outcome[0], 'plan')
        self.assertTrue(outcome[3].ok, outcome[3])
        self.assertNotEqual(outcome[2], goal)
        self.assertFalse(path_blocked(grid, outcome[3].points, config))
        self.assertEqual(nav.exploration.stats()['failed_targets'], 1)
        # An explicit Navigate destination retains its meaning and rejection.
        explicit = nav._plan(lambda: snapshot, (x, z), goal, False)[3]
        self.assertEqual(explicit.reason, 'goal_occupied')


class FrontierFailureRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_frontier_failure_waits_zero_then_fresh_planning_resumes_same_generation(self):
        for prototype in (False, True):
            for reason in ('goal_occupied', 'goal_unknown'):
                with self.subTest(prototype=prototype, reason=reason):
                    occupancy = PolicyOccupancy(corridor(), prototype)
                    h = Harness(Rover(2.5, 1., 0., moves=False), occupancy, mode='explore', no_progress_s=10.)
                    try:
                        failed = lambda *args, **kwargs: ('plan', occupancy.snapshot(), (2.5, 3.9), PlanResult([], reason))
                        with patch.object(h.nav, '_plan', side_effect=failed):
                            h.nav.start_explore(h.generation)
                            await wait_until(lambda: h.nav.waiting_reason == reason or not h.armed, timeout=1.)
                            self.assertTrue(h.armed, h.stops)
                            self.assertEqual(h.stops, [])
                            self.assertEqual(h.nav.path, [])
                            self.assertEqual(h.rover.commands[-1], (0., 0.))
                            self.assertFalse(any(v or w for v, w in h.rover.commands))
                        generation = h.generation
                        await wait_until(lambda: bool(h.nav.path) and any(v or w for v, w in h.rover.commands), timeout=2.)
                        self.assertEqual(h.generation, generation)
                        self.assertTrue(h.armed)
                        self.assertEqual(h.stops, [])
                    finally:
                        await h.nav.aclose()

    async def test_operator_stop_during_frontier_wait_cannot_resume(self):
        occupancy = PolicyOccupancy(corridor(), True)
        h = Harness(Rover(2.5, 1., 0., moves=False), occupancy, mode='explore', no_progress_s=10.)
        try:
            with patch.object(h.nav, '_plan', return_value=('plan', occupancy.snapshot(), (2.5, 3.9), PlanResult([], 'goal_occupied'))):
                h.nav.start_explore(h.generation)
                await wait_until(lambda: h.nav.waiting_reason == 'goal_occupied')
                self.assertTrue(h.armed)
                old = h.generation
                h.stop('operator_stop')
            await h.finished()
            self.assertFalse(h.armed)
            self.assertGreater(h.generation, old)
            self.assertEqual(h.stops, ['operator_stop'])
            self.assertEqual(h.nav.path, [])
            self.assertEqual(h.rover.commands[-1], (0., 0.))
        finally:
            await h.nav.aclose()

    async def test_explicit_destination_failure_still_stops_navigation(self):
        for reason, stop in (('goal_occupied', 'destination_blocked'), ('goal_unknown', 'destination_unknown')):
            with self.subTest(reason=reason):
                occupancy = PolicyOccupancy(corridor(), False)
                h = Harness(Rover(2.5, 1., 0., moves=False), occupancy)
                try:
                    with patch.object(h.nav, '_plan', return_value=('plan', occupancy.snapshot(), (2.5, 3.9), PlanResult([], reason))):
                        h.nav.start_goal((2.5, 3.9), None, h.generation)
                        await wait_until(lambda: not h.armed)
                    self.assertEqual(h.stops, [stop])
                    self.assertEqual(h.rover.commands[-1], (0., 0.))
                finally:
                    await h.nav.aclose()
