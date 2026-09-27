"""Real Explore/planner/follower, with a matched 10 Hz clock and kinematic plant.

The plant follows requested (v, w); it does not model prototype PWM motor arcs.
"""
import asyncio
import math
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

import backend.navigator as navigator
from backend.prototype import PrototypeActuation, prototype_geometry
from backend.tests.test_navigator import FakeOccupancy, Harness, Rover


class Clock:
    value = 1000.

    def now(self):
        return self.value


class TickLoop:
    def __init__(self, clock):
        self.clock = clock
        self.workers = 0

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, period):
        # Host CPU scheduling is not sensor latency. Finish each real worker
        # without advancing simulated time; every control/plant step stays .1 s.
        await asyncio.sleep(0)
        while self.workers:
            await asyncio.sleep(.001)
        self.clock.value += period
        await asyncio.sleep(.001)

    async def to_thread(self, func, *args):
        self.workers += 1
        try:
            return await asyncio.to_thread(func, *args)
        finally:
            self.workers -= 1


class FreshOccupancy(FakeOccupancy):
    def __init__(self, cells, clock):
        super().__init__(cells)
        self.clock = clock
        self.age_s = 0.

    def snapshot(self):
        return replace(super().snapshot(), accepted_at=self.clock.now() - self.age_s,
                       unknown_traversable=True,
                       inflation_m=prototype_geometry(.2286, .127).inflation_m)


def hallway():
    cells = np.ones((100, 100), np.uint8)
    cells[:3] = cells[:, :3] = cells[:, -3:] = 2
    cells[85:, 3:-3] = 0
    cells[38:50, 44:56] = 2
    return cells


@asynccontextmanager
async def exploring(cells=None, *, start=(2.5, .8, 0.), moves=True):
    clock = Clock()
    occupancy = FreshOccupancy(hallway() if cells is None else cells, clock)
    rover = Rover(*start, moves=moves, sim_dt=.1)
    h = Harness(rover, occupancy, mode='explore', follower=PrototypeActuation().follower(),
                rate_hz=10., replan_s=4., blocked_check_s=.25, no_progress_s=5.,
                start_recovery_margin_m=.1524)
    with patch.object(navigator, 'time', SimpleNamespace(monotonic=clock.now)), \
            patch.object(navigator, 'asyncio', TickLoop(clock)):
        h.nav.start_explore(h.generation)
        try:
            yield h, clock
        finally:
            await h.nav.aclose()


async def advance(h, clock, seconds, until=lambda: False):
    deadline = clock.now() + seconds
    while h.nav.active and clock.now() < deadline and not until():
        await asyncio.sleep(.005)


class DetourFollowingTests(unittest.IsolatedAsyncioTestCase):
    async def test_feasible_detour_makes_sustained_safe_progress(self):
        for mirrored in (False, True):
            with self.subTest(mirrored=mirrored):
                cells = hallway()
                cells[:, 72:] = 2  # leave only a roomy left-hand detour
                if mirrored:
                    cells = np.fliplr(cells).copy()
                async with exploring(cells) as (h, clock):
                    await advance(h, clock, 40., until=lambda: h.rover.z > 4.)
                    self.assertGreater(h.rover.z, 4.,
                                       (h.rover.x, h.rover.z, h.stops, len(h.paths)))
                    self.assertEqual(h.stops, [])
                    snapshot = h.occupancy.snapshot()
                    self.assertTrue(all(snapshot.traversable(x, z) for x, z in h.rover.trace))
                    turns = np.cumsum([w * .1 for _, w in h.rover.commands])
                    self.assertLess(max(abs(turns)), math.pi / 2,
                                    'nearby forward detour should not reverse the entry bearing')

    async def test_original_wide_hallway_detour_passes(self):
        async with exploring() as (h, clock):
            await advance(h, clock, 40., until=lambda: h.rover.z > 4.)
            self.assertGreater(h.rover.z, 4.)
            self.assertEqual(h.stops, [])
            self.assertTrue(all(h.occupancy.snapshot().traversable(x, z) for x, z in h.rover.trace))

    async def test_new_obstacle_during_explore_is_passed_on_a_safe_detour(self):
        initial = hallway()
        initial[38:50, 44:56] = 1
        async with exploring(initial) as (h, clock):
            def reveal(rover):
                if rover.z > 1.1 and h.occupancy.revision == 1:
                    h.occupancy.set(hallway())
            h.rover.on_move = reveal
            await advance(h, clock, 40., until=lambda: h.rover.z > 4.)
            self.assertEqual(h.occupancy.revision, 2)
            self.assertGreater(h.rover.z, 4.)
            self.assertEqual(h.stops, [])
            self.assertTrue(all(h.occupancy.snapshot().traversable(x, z) for x, z in h.rover.trace))

    async def test_free_explore_continues_beyond_its_internal_waypoint_as_sensing_expands(self):
        async with exploring() as (h, clock):
            await advance(h, clock, 2., until=lambda: bool(h.nav.path))
            first_frontier = h.nav.path[-1]
            def reveal(rover):
                if rover.z > 3.6 and h.occupancy.revision == 1:
                    cells = np.pad(hallway(), ((0, 20), (0, 0)), constant_values=1)
                    cells[85:, 3:-3] = 1
                    cells[110:, 3:-3] = 0
                    cells[:, :3] = cells[:, -3:] = 2
                    h.occupancy.set(cells)
            h.rover.on_move = reveal
            await advance(h, clock, 55., until=lambda: h.rover.z > 5.)
            self.assertGreater(h.rover.z, 5.)
            self.assertGreater(h.rover.z, first_frontier[1] + .5)
            self.assertGreater(h.nav.goal[1], first_frontier[1] + .75)
            self.assertTrue(h.nav.active)
            self.assertEqual(h.stops, [])
            self.assertTrue(all(h.occupancy.snapshot().traversable(x, z) for x, z in h.rover.trace))

    async def test_bent_route_passes_two_obstacles_and_straight_route_keeps_progress(self):
        for scene in ('straight', 'bent', 'mirrored_bent'):
            with self.subTest(scene=scene):
                cells = hallway()
                cells[:, 72:] = 2
                if scene == 'straight':
                    cells[38:50, 44:56] = 1
                else:
                    cells = np.pad(cells, ((0, 20), (0, 0)), constant_values=1)
                    cells[85:, 3:72] = 1
                    cells[110:, 3:72] = 0
                    cells[:, 72:] = cells[:, :3] = 2
                    cells[75:81, 3:44] = 2  # second obstacle requires the other side
                if scene == 'mirrored_bent':
                    cells = np.fliplr(cells).copy()
                async with exploring(cells) as (h, clock):
                    target_z = 4. if scene == 'straight' else 5.
                    await advance(h, clock, 80., until=lambda: h.rover.z > target_z)
                    self.assertGreater(h.rover.z, target_z, (scene, h.rover.x, h.rover.z, h.stops))
                    self.assertEqual(h.stops, [])
                    self.assertTrue(all(h.occupancy.snapshot().traversable(x, z) for x, z in h.rover.trace))
                    if scene == 'straight':
                        self.assertTrue(any(v >= .15 for v, _ in h.rover.commands))

    async def test_narrow_passage_without_a_route_remains_at_zero(self):
        cells = hallway()
        cells[38:50, :46] = cells[38:50, 54:] = 2  # .4 m gap cannot fit the inflated rover
        async with exploring(cells) as (h, clock):
            await advance(h, clock, 12.)
            self.assertEqual(h.stops, [])
            self.assertEqual(h.nav.waiting_reason, 'explore_complete')
            self.assertEqual(set(h.rover.commands), {(0., 0.)})
            self.assertEqual(h.stops, [])
            self.assertTrue(h.nav.active)
            h.occupancy.age_s = 2.
            await advance(h, clock, 2.)
            self.assertEqual(h.stops, ['sensing_stale'])
            self.assertFalse(h.armed)

    def test_predicted_arcs_preserve_strict_clearance_and_monotonic_overlap_exit(self):
        cells = np.ones((120, 60), np.uint8)
        cells[:, 38:] = 2  # obstacle starts x=1.9 m
        snapshot = FreshOccupancy(cells, Clock()).snapshot()
        allowed = navigator.pursuit_step_allowed
        self.assertTrue(snapshot.traversable(1.45, 1.))
        self.assertFalse(allowed(snapshot, 1.45, 1., math.pi / 2, .15, 0., .1, .6, .1524))
        self.assertFalse(allowed(snapshot, 1.485, 1., math.pi / 2, .15, 0., .1, .15, .1524))
        self.assertFalse(snapshot.traversable(1.5, 1.))
        self.assertTrue(allowed(snapshot, 1.5, 1., -math.pi / 2, .15, 0., .1, .6, .1524))
        self.assertFalse(allowed(snapshot, 1.5, 1., math.pi / 2, .15, 0., .1, .15, .1524))
        self.assertFalse(allowed(snapshot, 1.7, 1., -math.pi / 2, .15, 0., .1, .6, .1524))

    async def test_rejected_action_waits_across_identical_replans_then_retries_new_evidence(self):
        async with exploring() as (h, clock):
            await advance(h, clock, 2., until=lambda: bool(h.nav.path))
            # Clear pose at the corner's margin with no supported pursuit arc,
            # retaining Explore's entry bearing and selected forward frontier.
            h.rover.x, h.rover.z, h.rover.yaw = 3.07, 1.58, .4
            count = len(h.rover.commands)
            await advance(h, clock, 12.)
            self.assertLessEqual(len(h.paths), 2, 'identical replans must not clear/reinstall the path')
            self.assertEqual(getattr(h.nav, 'waiting_reason', None), 'no_feasible_step')
            self.assertEqual(set(h.rover.commands[count:]), {(0., 0.)})
            self.assertTrue(h.nav.active)
            self.assertEqual(h.stops, [])
            opened = h.occupancy.cells.copy()
            opened[38:50, 44:56] = 1
            h.occupancy.set(opened)
            await advance(h, clock, 20., until=lambda: h.rover.z > 3.3)
            self.assertGreater(h.rover.z, 3.3)
            self.assertIsNone(h.nav.waiting_reason)
            self.assertEqual(h.stops, [])

    async def test_waiting_cannot_resume_after_stop_stale_pose_tracking_or_generation_loss(self):
        for reason in ('operator_stop', 'pose_stale', 'tracking_lost', 'sensing_stale', 'command_stale'):
            with self.subTest(reason=reason):
                async with exploring() as (h, clock):
                    await advance(h, clock, 2., until=lambda: bool(h.nav.path))
                    h.rover.x, h.rover.z, h.rover.yaw = 3.07, 1.58, .4
                    await advance(h, clock, 2., until=lambda: h.nav.waiting_reason == 'no_feasible_step')
                    self.assertEqual(h.nav.waiting_reason, 'no_feasible_step')
                    if reason == 'operator_stop':
                        h.stop(reason)
                    elif reason == 'pose_stale':
                        h.rover.age_s = 2.
                    elif reason == 'tracking_lost':
                        h.rover.tracking = 'limited'
                    elif reason == 'sensing_stale':
                        h.occupancy.age_s = 2.
                    else:
                        h.generation += 1
                    await advance(h, clock, 2.)
                    self.assertEqual(h.stops, [reason])
                    self.assertFalse(h.armed)
                    self.assertEqual(h.nav.path, [])
                    self.assertIsNone(h.nav.waiting_reason)
                    count = len(h.rover.commands)
                    h.rover.age_s = h.occupancy.age_s = 0.
                    h.rover.tracking = 'normal'
                    h.occupancy.set(hallway())
                    await asyncio.sleep(.02)
                    self.assertEqual(h.rover.commands[count:], [])


if __name__ == '__main__':
    unittest.main()
