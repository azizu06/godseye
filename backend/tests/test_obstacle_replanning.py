"""Explore obstacle recovery with real measured-grid fixtures; no hardware actuation."""
import asyncio
import time
import threading
import unittest

import numpy as np

from backend.calibration import RoverCalibration
from backend.navigation import path_blocked
from backend.navigator import planning_grid
from backend.occupancy import OccupancyGrid, frame_evidence
from backend.tests.test_calibration import TEST_CALIBRATION
from backend.tests.test_navigator import Harness, Rover, wait_until
from backend.tests.test_occupancy import FLOOR_Y, box, plane


class ObservedRoom:
    """Ideal observed geometry for policy tests, not a mounted-camera visibility claim."""
    def __init__(self):
        self.grid = OccupancyGrid(('obstacle-test', 1),
                                  calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        self.points = plane(0, 4, 0, 4, FLOOR_Y, step=.04)
        self.feed()

    def feed(self):
        for _ in range(3):
            self.grid.add(self.points, now=time.monotonic())

    def obstacle(self, x0, x1, z0, z1):
        self.points = np.concatenate((self.points, box(x0, x1, z0, z1, FLOOR_Y, FLOOR_Y + .4, step=.04)))
        self.feed()

    def snapshot(self):
        return self.grid.map_snapshot()

    async def sensing(self):
        while True:
            self.grid.add(self.points, now=time.monotonic())
            await asyncio.sleep(.04)


class ObstaclePlanningTests(unittest.TestCase):
    def test_clear_detour_keeps_the_current_explore_intent(self):
        room = ObservedRoom()
        room.obstacle(1.8, 2.2, 1.8, 2.1)
        h = Harness(Rover(2., 1., 0.), room, mode='explore')
        goal = (2., 3.5)
        kind, snapshot, target, plan = h.nav._plan(room.snapshot, (2., 1.), goal, True, 0., 0.)
        self.assertEqual(kind, 'plan')
        self.assertTrue(plan.ok, plan.reason)
        self.assertEqual(target, goal)
        self.assertTrue(any(abs(x - 2.) > .4 for x, z in plan.points if 1.5 < z < 2.5))
        grid, config = planning_grid(snapshot, h.nav.settings)
        self.assertFalse(path_blocked(grid, plan.points, config))

    def test_new_wall_retires_only_unreachable_implicit_frontier(self):
        room = ObservedRoom()
        h = Harness(Rover(2., 1., 0.), room, mode='explore')
        initial = h.nav._plan(room.snapshot, (2., 1.), None, True, 0., 0.)
        goal = initial[2]
        self.assertGreater(goal[1], 2.1)
        room.obstacle(0., 4., 2., 2.1)
        self.assertTrue(room.snapshot().traversable(*goal))  # free, but now disconnected
        outcome = h.nav._plan(room.snapshot, (2., 1.), goal, True, 0., 0.)
        self.assertEqual(outcome[0], 'plan')
        _, snapshot, target, plan = outcome
        self.assertTrue(plan.ok, plan.reason)
        self.assertNotEqual(target, goal)
        self.assertLess(target[1], 2.)
        grid, config = planning_grid(snapshot, h.nav.settings)
        self.assertFalse(path_blocked(grid, plan.points, config))
        # A selected destination is not an implicit frontier: its behavior stays unchanged.
        fixed = h.nav._plan(room.snapshot, (2., 1.), goal, False)
        self.assertEqual(fixed[3].reason, 'no_path')


class ObstacleRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_wall_turns_toward_reachable_region(self):
        room = ObservedRoom()
        rover = Rover(2., 1., 0., sim_dt=.04)
        h = Harness(rover, room, mode='explore', rate_hz=40., replan_s=4., blocked_check_s=.05)
        sensing = asyncio.create_task(room.sensing())
        try:
            h.nav.start_explore(h.generation)
            await wait_until(lambda: bool(h.nav.path))
            old_goal = h.nav.goal
            room.obstacle(0., 4., 2., 2.1)
            before = len(rover.commands)
            await wait_until(lambda: h.nav.goal != old_goal or bool(h.stops), timeout=2.)
            self.assertEqual(h.stops, [])
            self.assertNotEqual(h.nav.goal, old_goal)
            await wait_until(lambda: any(w != 0 for _, w in rover.commands[before:]))
            self.assertTrue(all(room.snapshot().traversable(x, z) for x, z in rover.trace))
        finally:
            await h.nav.aclose()
            sensing.cancel()
            await asyncio.gather(sensing, return_exceptions=True)

    async def test_no_reachable_frontier_keeps_zero_with_fresh_sensing_but_stops_on_real_loss(self):
        room = ObservedRoom()
        room.obstacle(0., 4., 0., .15)
        room.obstacle(0., 4., 3.85, 4.)
        room.obstacle(0., .15, 0., 4.)
        room.obstacle(3.85, 4., 0., 4.)
        h = Harness(Rover(2., 1., 0.), room, mode='explore', rate_hz=40., replan_s=4.,
                    blocked_check_s=.05, map_max_age_s=.3)
        sensing = asyncio.create_task(room.sensing())
        try:
            self.assertEqual(h.nav._plan(room.snapshot, (2., 1.), None, True)[0], 'explore_complete')
            h.nav.start_explore(h.generation)
            await asyncio.sleep(.75)  # older than cached snapshot limit, shorter than full replan
            self.assertEqual(h.stops, [])
            self.assertTrue(h.nav.active)
            self.assertTrue(h.rover.commands)
            self.assertTrue(all(command == (0., 0.) for command in h.rover.commands))
            sensing.cancel()
            await asyncio.gather(sensing, return_exceptions=True)
            await h.finished(timeout=1.)
            self.assertEqual(h.stops, ['sensing_stale'])
        finally:
            await h.nav.aclose()
            sensing.cancel()
            await asyncio.gather(sensing, return_exceptions=True)


    async def test_current_mesh_blocker_holds_zero_then_new_clear_map_resumes(self):
        room = ObservedRoom()
        blocker = frame_evidence(box(1.9, 2.1, 1.05, 1.2, FLOOR_Y, FLOOR_Y + .4, step=.04))
        room.grid.commit(frame_evidence(room.points), time.monotonic(), mesh_keys=blocker.keys)
        h = Harness(Rover(2., 1., 0.), room, mode='explore', rate_hz=40., replan_s=.6,
                    blocked_check_s=.04, map_max_age_s=.2)
        sensing = asyncio.create_task(room.sensing())
        try:
            self.assertFalse(room.snapshot().traversable(2., 1.))
            h.nav.start_explore(h.generation)
            await asyncio.sleep(.4)
            self.assertEqual(h.stops, [])
            self.assertTrue(h.rover.commands)
            self.assertTrue(all(command == (0., 0.) for command in h.rover.commands))
            # Actual current-mesh replacement, not a TTL or forged free padding.
            room.grid.commit(frame_evidence(room.points), time.monotonic(), mesh_keys=[])
            self.assertTrue(room.snapshot().traversable(2., 1.))
            await wait_until(lambda: any(v or w for v, w in h.rover.commands), timeout=1.)
            self.assertEqual(h.stops, [])
        finally:
            await h.nav.aclose()
            sensing.cancel()
            await asyncio.gather(sensing, return_exceptions=True)

    async def test_stop_or_generation_change_cannot_publish_late_obstacle_replan(self):
        for action in ('operator_stop', 'session_reset', 'phone_disconnect', 'generation'):
            with self.subTest(action=action):
                room = ObservedRoom()
                h = Harness(Rover(2., 1., 0., moves=False), room, mode='explore',
                            rate_hz=40., replan_s=4., blocked_check_s=.04)
                sensing = asyncio.create_task(room.sensing())
                started, release = threading.Event(), threading.Event()
                plan = h.nav._plan
                calls = 0

                def delayed(*args):
                    nonlocal calls
                    calls += 1
                    result = plan(*args)
                    if calls == 2:
                        started.set()
                        release.wait(3.)
                    return result

                h.nav._plan = delayed
                try:
                    h.nav.start_explore(h.generation)
                    await wait_until(lambda: bool(h.nav.path))
                    room.obstacle(0., 4., 2., 2.1)
                    await wait_until(started.is_set)
                    if action == 'generation':
                        h.generation += 1
                        await h.finished(timeout=1.)
                    else:
                        h.stop(action)  # app Stop/reset/disconnect all invoke this halt boundary
                    count = len(h.rover.commands)
                    publications = len(h.paths)
                    release.set()
                    await asyncio.sleep(.15)
                    self.assertFalse(h.nav.active)
                    self.assertEqual(h.nav.path, [])
                    self.assertEqual(h.rover.commands[count:], [])
                    self.assertEqual(h.paths[publications:], [])
                    self.assertEqual(h.stops, ['command_stale' if action == 'generation' else action])
                finally:
                    release.set()
                    await h.nav.aclose()
                    sensing.cancel()
                    await asyncio.gather(sensing, return_exceptions=True)
