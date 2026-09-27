"""Continuous driving regressions: real planner, follower and asynchronous runner."""
import asyncio
import math
import threading
import time
import unittest
from dataclasses import replace

import numpy as np

from backend.navigation import FollowerConfig, Grid, PlannerConfig, PurePursuit, plan_path
from backend.navigator import NavSettings, Navigator, RoverPose
from backend.occupancy import OccupancySnapshot
from backend.tests.test_navigator import FakeOccupancy, Harness, Rover, cells, initial_plan, wait_until


class SteeringTests(unittest.TestCase):
    def test_pivot_finishes_alignment_before_resuming_translation(self):
        follower = PurePursuit([(0., 0.), (0., 3.)],
                              FollowerConfig(rotate_in_place_rad=.6))
        self.assertEqual(follower.step(0., 0., 1.).status, 'rotate')
        self.assertEqual(follower.step(0., 0., .59).status, 'rotate')
        self.assertEqual(follower.step(0., 0., .1).status, 'follow')

    def test_replan_does_not_cancel_an_unfinished_pivot(self):
        follower = PurePursuit([(0., 0.), (0., 3.)],
                              FollowerConfig(rotate_in_place_rad=.6))
        follower.step(0., 0., 1.)
        follower = follower.replaced([(0., 0.), (0., 4.)])
        self.assertEqual(follower.step(0., 0., .5).status, 'rotate')


class ClearanceTests(unittest.TestCase):
    def test_detour_leaves_space_for_steering_at_obstacle_corner(self):
        a = np.ones((100, 100), np.uint8)
        a[40:60, 40:60] = 2
        grid = Grid.from_array(a, origin=(0., 0.), cell_m=.05)
        config = PlannerConfig(robot_radius_m=.2, margin_m=0.,
                               clearance_weight=4., preferred_clearance_m=.3)
        plan = plan_path(grid, (2.5, .5), (2.5, 4.5), config)
        self.assertTrue(plan.ok, plan.reason)
        # At the side of the block, the route should use available room for
        # tracking error instead of skimming the hard .2 m inflation boundary.
        passing = [x for x, z in plan.points if 2.1 < z < 2.9]
        self.assertTrue(passing)
        self.assertTrue(all(x < 1.65 or x > 3.35 for x in passing), plan.points)


class ContinuousRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tight_corner_recovers_by_aligning_to_nearby_route(self):
        from backend.prototype import PrototypeActuation
        xx, zz = np.meshgrid((np.arange(100) + .5) * .05, (np.arange(100) + .5) * .05)
        free = (((xx > .5) & (xx < 1.25) & (zz > .5) & (zz < 4.5)) |
                ((xx > .5) & (xx < 4.5) & (zz > 3.75) & (zz < 4.5)))
        class TightOccupancy(FakeOccupancy):
            def snapshot(self):
                return replace(super().snapshot(), inflation_m=.3, unknown_traversable=True)
        occupancy = TightOccupancy(np.where(free, 1, 2))
        start = (.9893729136392488, 3.899195950605866)
        rover = Rover(*start, .5992653576378062)
        h = Harness(rover, occupancy, follower=PrototypeActuation().follower(),
                    start_recovery_margin_m=.1524, no_progress_s=.5)
        goal = (4., 4.125)
        initial = h.nav._plan(occupancy.snapshot, start, goal, False)[3]
        self.assertTrue(initial.ok, initial.reason)
        h.nav.start_goal(goal, initial, 1)
        try:
            await h.finished()
            self.assertEqual(h.stops, ['arrived'])
            self.assertTrue(any(v == 0. and w != 0. for v, w in rover.commands))
        finally:
            await h.nav.aclose()

    async def test_obsolete_failed_plan_cannot_stop_newly_clear_route(self):
        await self.obsolete_plan_result(stale_sensing=False)

    async def test_obsolete_sensing_failure_cannot_override_fresh_identical_map(self):
        await self.obsolete_plan_result(stale_sensing=True)

    async def obsolete_plan_result(self, *, stale_sensing):
        occupancy = FakeOccupancy(cells())
        start, goal = (.5, .5), (2.5, .5)
        initial = initial_plan(occupancy.cells, start, goal)
        blocked = cells()
        if not stale_sensing:
            blocked[:, 29:31] = 2
        occupancy.set(blocked)
        h = Harness(Rover(*start, math.pi / 2, moves=False), occupancy,
                    replan_s=10., no_progress_s=10.)
        captured, release = threading.Event(), threading.Event()
        original_plan, original_check = h.nav._plan, h.nav._check
        latest_checked = []
        def delayed_failure(*args, **kwargs):
            if stale_sensing and not captured.is_set():
                source = lambda: replace(occupancy.snapshot(), accepted_at=time.monotonic() - 5.)
                result = original_plan(source, *args[1:], **kwargs)
            else:
                result = original_plan(*args, **kwargs)
            if not captured.is_set():
                captured.set()
                release.wait(2.)
            return result
        def check(*args, **kwargs):
            result = original_check(*args, **kwargs)
            latest_checked.append(result[1].revision)
            return result
        h.nav._plan, h.nav._check = delayed_failure, check
        h.nav.start_goal(goal, initial, 1)
        try:
            await wait_until(captured.is_set)
            occupancy.set(cells())
            await wait_until(lambda: 3 in latest_checked)
            release.set()
            await asyncio.sleep(.15)
            self.assertTrue(h.nav.active, h.stops)
            self.assertTrue(any(v > 0 for v, _ in h.rover.commands))
        finally:
            release.set()
            await h.nav.aclose()

    async def test_slow_replanning_does_not_make_live_sensing_stale(self):
        occupancy = FakeOccupancy(cells(size=100))
        rover = Rover(.5, .5, 0.)
        h = Harness(rover, occupancy, replan_s=.06, blocked_check_s=.015,
                    map_max_age_s=.12, rate_hz=100.)
        original = h.nav._plan
        def slow_plan(*args, **kwargs):
            time.sleep(.25)
            return original(*args, **kwargs)
        h.nav._plan = slow_plan
        goal = (.5, 4.)
        h.nav.start_goal(goal, initial_plan(occupancy.cells, (.5, .5), goal), 1)
        try:
            await asyncio.sleep(.65)
            self.assertTrue(h.nav.active, h.stops)
            self.assertGreater(rover.z, .8)
        finally:
            await h.nav.aclose()

    async def test_goal_replans_around_obstacle_without_disarming(self):
        occupancy = FakeOccupancy(cells(size=100))
        start, goal = (2.5, .5), (2.5, 4.5)
        def reveal(rover):
            if rover.z > .8 and occupancy.revision == 1:
                a = occupancy.cells.copy()
                a[40:60, 43:57] = 2
                occupancy.set(a)
        h = Harness(Rover(*start, 0., on_move=reveal), occupancy,
                    replan_s=100., blocked_check_s=.02)
        h.nav.start_goal(goal, initial_plan(occupancy.cells, start, goal), 1)
        try:
            await h.finished()
            self.assertEqual(h.stops, ['arrived'])
            self.assertTrue(any(abs(x - 2.5) > .5 for x, z in h.rover.trace if 2. < z < 3.))
        finally:
            await h.nav.aclose()
