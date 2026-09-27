"""Hallway exploration should take a central straight route before side branches."""
import asyncio
import time
import unittest

import numpy as np

from backend.navigation import path_blocked
from backend.navigator import Navigator, NavSettings, RoverPose, planning_grid, recoverable_start, recovery_step_allowed
from backend.navigator import straight_runway_m
from backend.occupancy import OccupancySnapshot
from backend.prototype import PrototypeActuation


class CorridorTests(unittest.TestCase):
    def test_prototype_can_plan_out_of_extra_clearance_near_a_wall(self):
        cells = np.ones((120, 60), np.uint8)
        cells[:, 38:] = 2  # wall begins at x=.4; rover center x=0
        cells[90:, :38] = 0
        snapshot = OccupancySnapshot(('hall', 1), 1, time.monotonic(), (), .414,
                                     (-1.5, 0.), .05, cells, 0., True)
        settings = NavSettings(start_recovery_margin_m=.1524)
        navigator = Navigator(settings, pose=lambda: None, occupancy=lambda: snapshot,
                              submit=lambda *_: True, stop=lambda _: None, publish=lambda _: None,
                              armed_mode=lambda: 'explore')
        self.assertFalse(snapshot.traversable(0., 1.))
        self.assertTrue(recoverable_start(snapshot, 0., 1., settings.start_recovery_margin_m))
        kind, _, goal, plan = navigator._plan(lambda: snapshot, (0., 1.), (-.3, 4.), False)
        self.assertEqual(kind, 'plan')
        self.assertTrue(plan.ok, plan.reason)
        self.assertGreater(goal[1], 2.)
        self.assertTrue(recovery_step_allowed(snapshot, 0., 1., -.01, 1.01,
                                              settings.start_recovery_margin_m))

    def test_prototype_does_not_recover_into_a_close_obstacle(self):
        cells = np.ones((120, 60), np.uint8)
        cells[:, 32:] = 2  # obstacle only .1 m from the rover center
        snapshot = OccupancySnapshot(('hall', 1), 1, time.monotonic(), (), .414,
                                     (-1.5, 0.), .05, cells, 0., True)
        self.assertFalse(recoverable_start(snapshot, 0., 1., .1524))
        self.assertFalse(recovery_step_allowed(snapshot, 0., 1., -.01, 1.01, .1524))

    def test_straight_leg_after_obstacle_can_regain_cruise_speed(self):
        path = [[1., 1.], [1., 1.4], [.65, 1.7], [.65, 2.], [.65, 2.5],
                [.65, 3.], [.65, 3.5]]
        self.assertLess(straight_runway_m(path, 0, 1., 1., 0.), .8)
        self.assertGreater(straight_runway_m(path, 3, .65, 2., 0.), 1.)

    def test_detour_replan_keeps_original_hallway_progress(self):
        cells = np.ones((110, 41), np.uint8)
        cells[:, :3] = cells[:, 38:] = 2
        cells[90:, 3:38] = 0
        cells[39:47, 18:23] = 2  # stand in the middle of the hallway
        snapshot = OccupancySnapshot(('hall', 1), 1, time.monotonic(), (), .18,
                                     (0., 0.), .05, cells, 0., True)
        navigator = Navigator(NavSettings(), pose=lambda: None, occupancy=lambda: snapshot,
                              submit=lambda *_: True, stop=lambda _: None, publish=lambda _: None,
                              armed_mode=lambda: 'explore')
        # The rover has turned beside the stand. Its new body yaw points back,
        # but the exploration target should remain down the original hallway.
        kind, _, goal, plan = navigator._plan(
            lambda: snapshot, (.6, 2.25), None, True, yaw=3.0, explore_yaw=0.)
        self.assertEqual(kind, 'plan')
        self.assertTrue(plan.ok, plan.reason)
        self.assertGreater(goal[1], 3.5)
        self.assertTrue(all(z > 1.8 for _, z in plan.points), plan.points)
        self.assertGreater(plan.points[-1][1], 3.5)

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
