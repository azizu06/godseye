"""Hallway exploration should take a central straight route before side branches."""
import asyncio
import time
import unittest

import numpy as np

from backend.navigation import path_blocked
from backend.navigator import Navigator, NavSettings, RoverPose, planning_grid
from backend.occupancy import OccupancySnapshot
from backend.prototype import PrototypeActuation


class CorridorTests(unittest.TestCase):
    def test_open_straight_route_can_cruise_before_both_walls_are_mapped(self):
        cells = np.ones((110, 41), np.uint8)
        cells[90:] = 0
        snapshot = OccupancySnapshot(('hall', 1), 1, time.monotonic(), (), .18,
                                     (0., 0.), .05, cells, 0., True)
        navigator = Navigator(NavSettings(), pose=lambda: None, occupancy=lambda: snapshot,
                              submit=lambda *_: True, stop=lambda _: None, publish=lambda _: None,
                              armed_mode=lambda: 'explore')
        _, _, _, plan = navigator._plan(lambda: snapshot, (1.025, 1.), None, True)
        self.assertTrue(plan.ok, plan.reason)
        self.assertTrue(plan.fast_corridor)

    def test_replan_extends_a_clear_forward_goal_as_new_depth_opens_the_hallway(self):
        cells = np.ones((110, 41), np.uint8)
        cells[:, :3] = cells[:, 38:] = 2
        cells[90:, 3:38] = 0
        snapshot = OccupancySnapshot(('hall', 1), 1, time.monotonic(), (), .18,
                                     (0., 0.), .05, cells, 0., True)
        navigator = Navigator(NavSettings(), pose=lambda: None, occupancy=lambda: snapshot,
                              submit=lambda *_: True, stop=lambda _: None, publish=lambda _: None,
                              armed_mode=lambda: 'explore')
        old_goal = (1.025, 2.)
        _, _, goal, plan = navigator._plan(lambda: snapshot, (1.025, 1.), old_goal, True)
        self.assertGreater(goal[1], old_goal[1] + 1.)
        self.assertAlmostEqual(goal[0], old_goal[0], delta=.15)
        self.assertTrue(plan.ok, plan.reason)
        self.assertTrue(plan.fast_corridor)

    def test_explore_moves_to_hallway_center_then_targets_far_unexplored_end(self):
        cells = np.ones((110, 41), np.uint8)
        cells[:, :3] = cells[:, 38:] = 2
        cells[90:, 3:38] = 0
        snapshot = OccupancySnapshot(('hall', 1), 1, time.monotonic(), (), .18,
                                     (0., 0.), .05, cells, 0., True)
        navigator = Navigator(NavSettings(), pose=lambda: None, occupancy=lambda: snapshot,
                              submit=lambda *_: True, stop=lambda _: None, publish=lambda _: None,
                              armed_mode=lambda: 'explore')
        kind, used, goal, plan = navigator._plan(lambda: snapshot, (.55, .5), None, True)
        self.assertEqual(kind, 'plan')
        self.assertTrue(plan.ok, plan.reason)
        self.assertGreater(goal[1], 4.)
        self.assertAlmostEqual(goal[0], 1.025, delta=.15)
        self.assertTrue(any(abs(x - 1.025) < .15 and z < 1.5 for x, z in plan.points), plan.points)
        self.assertFalse(plan.fast_corridor, 'move to the middle before faster straight travel')
        grid, config = planning_grid(used, navigator.settings)
        self.assertFalse(path_blocked(grid, plan.points, config))

        _, _, centered_goal, centered = navigator._plan(lambda: snapshot, (1.025, 1.0), None, True)
        self.assertGreater(centered_goal[1], 4.)
        self.assertTrue(centered.fast_corridor)

    def test_upcoming_center_obstacle_disables_fast_straight_command(self):
        cells = np.ones((110, 41), np.uint8)
        cells[:, :3] = cells[:, 38:] = 2
        cells[90:, 3:38] = 0
        cells[42:50, 18:23] = 2  # obstacle beyond the half-meter hallway probe
        snapshot = OccupancySnapshot(('hall', 1), 1, time.monotonic(), (), .18,
                                     (0., 0.), .05, cells, 0., True)
        navigator = Navigator(NavSettings(), pose=lambda: None, occupancy=lambda: snapshot,
                              submit=lambda *_: True, stop=lambda _: None, publish=lambda _: None,
                              armed_mode=lambda: 'explore')
        _, _, _, plan = navigator._plan(lambda: snapshot, (1.025, 1.), None, True)
        self.assertTrue(plan.ok, plan.reason)
        self.assertFalse(plan.fast_corridor)

    def test_turns_toward_unexplored_side_room_when_hallway_ends(self):
        cells = np.ones((95, 70), np.uint8)
        cells[:, :3] = cells[:, 38:] = 2
        cells[40:60, 38:] = 0  # doorway into an unmapped side room
        cells[78:95, 3:38] = 2  # closed far end of the hallway
        snapshot = OccupancySnapshot(('hall', 1), 1, time.monotonic(), (), .18,
                                     (0., 0.), .05, cells, 0., True)
        navigator = Navigator(NavSettings(), pose=lambda: None, occupancy=lambda: snapshot,
                              submit=lambda *_: True, stop=lambda _: None, publish=lambda _: None,
                              armed_mode=lambda: 'explore')
        _, _, goal, plan = navigator._plan(lambda: snapshot, (1.025, 1.), None, True)
        self.assertTrue(plan.ok, plan.reason)
        self.assertGreater(goal[0], 1.5)
        self.assertGreater(goal[1], 1.8)
        self.assertFalse(plan.fast_corridor)


class CorridorRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_centered_hallway_submits_fast_straight_command(self):
        cells = np.ones((110, 41), np.uint8)
        cells[:, :3] = cells[:, 38:] = 2
        cells[90:, 3:38] = 0
        snapshot = OccupancySnapshot(('hall', 1), 1, time.monotonic(), (), .18,
                                     (0., 0.), .05, cells, 0., True)
        commands = []
        navigator = Navigator(NavSettings(rate_hz=50., follower=PrototypeActuation().follower()),
                              pose=lambda: RoverPose(1.025, 1., 0., 0., 'normal'),
                              occupancy=lambda: snapshot,
                              submit=lambda _, __, v, w: commands.append((v, w)) or True,
                              stop=lambda _: None, publish=lambda _: None,
                              armed_mode=lambda: 'explore')
        navigator.start_explore(1)
        try:
            await asyncio.sleep(.4)
            self.assertIn((.2, 0.), commands)
        finally:
            await navigator.aclose()
