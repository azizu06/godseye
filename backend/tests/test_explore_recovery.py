"""Explore recovery through the real runner, fresh synthetic maps and fake motion."""
import asyncio
import math
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace

import numpy as np

from backend.navigation import path_blocked
from backend.navigator import planning_grid
from backend.tests.test_navigator import FakeOccupancy, Harness, Rover, wait_until


class PolicyOccupancy(FakeOccupancy):
    def __init__(self, cells, prototype):
        super().__init__(cells)
        self.prototype = prototype
        self.age_s = 0.

    def snapshot(self):
        snapshot = super().snapshot()
        return replace(snapshot, accepted_at=snapshot.accepted_at - self.age_s,
                       unknown_traversable=self.prototype)


def corridor():
    cells = np.ones((100, 100), np.uint8)
    cells[:3] = cells[:, :3] = cells[:, -3:] = 2
    cells[80:, 3:-3] = 0
    return cells


@asynccontextmanager
async def exploring(prototype):
    occupancy = PolicyOccupancy(corridor(), prototype)
    h = Harness(Rover(2.5, 1., 0., moves=False), occupancy,
                mode='explore', no_progress_s=10.)
    try:
        h.nav.start_explore(h.generation)
        await wait_until(lambda: bool(h.nav.path))
        yield h
    finally:
        await h.nav.aclose()


def seal_selected_frontier(occupancy, *, side_open):
    changed = occupancy.cells.copy()
    changed[45:50, :] = 2
    if side_open:
        changed[12:35, :3] = 0
    occupancy.set(changed)


class ExploreRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_selected_frontier_yields_to_reachable_side_frontier(self):
        for prototype in (False, True):
            with self.subTest(prototype=prototype):
                async with exploring(prototype) as h:
                    selected = h.nav.path[-1]
                    self.assertGreater(selected[1], 3.5)
                    seal_selected_frontier(h.occupancy, side_open=True)
                    snapshot = h.occupancy.snapshot()
                    self.assertTrue(snapshot.traversable(*selected))
                    await wait_until(lambda: bool(h.nav.path) and math.dist(h.nav.path[-1], selected) > .25,
                                     timeout=1.)

                    self.assertLess(h.nav.path[-1][0], 1.)
                    self.assertLess(h.nav.path[-1][1], 2.)
                    grid, config = planning_grid(snapshot, h.nav.settings)
                    self.assertFalse(path_blocked(grid, h.nav.path, config))
                    count = len(h.rover.commands)
                    await wait_until(lambda: any(v or w for v, w in h.rover.commands[count:]))
                    self.assertTrue(h.armed)
                    self.assertEqual(h.stops, [])

    async def test_reachable_detour_keeps_selected_frontier(self):
        for prototype in (False, True):
            with self.subTest(prototype=prototype):
                async with exploring(prototype) as h:
                    original = h.nav.path
                    changed = h.occupancy.cells.copy()
                    changed[35:45, 46:54] = 2  # obstacle with clear space on both sides
                    h.occupancy.set(changed)
                    await wait_until(lambda: bool(h.nav.path) and h.nav.path != original)
                    for actual, expected in zip(h.nav.path[-1], original[-1]):
                        self.assertAlmostEqual(actual, expected, places=7)
                    grid, config = planning_grid(h.occupancy.snapshot(), h.nav.settings)
                    self.assertFalse(path_blocked(grid, h.nav.path, config))
                    self.assertEqual(h.stops, [])

    async def test_closed_region_waits_at_zero_when_selected_frontier_is_cut_off(self):
        for prototype in (False, True):
            with self.subTest(prototype=prototype):
                async with exploring(prototype) as h:
                    seal_selected_frontier(h.occupancy, side_open=False)
                    await wait_until(lambda: not h.nav.path and h.rover.commands[-1] == (0., 0.))
                    count = len(h.rover.commands)
                    await asyncio.sleep(.15)  # several full replans of the enclosed region
                    self.assertFalse(any(v or w for v, w in h.rover.commands[count:]))
                    self.assertTrue(h.armed)
                    self.assertEqual(h.stops, [])

    async def test_recovery_cannot_resume_after_stop_or_stale_evidence(self):
        for prototype in (False, True):
            for reason in ('operator_stop', 'sensing_stale', 'pose_stale'):
                with self.subTest(prototype=prototype, reason=reason):
                    async with exploring(prototype) as h:
                        seal_selected_frontier(h.occupancy, side_open=True)
                        if reason == 'operator_stop':
                            h.stop(reason)
                        elif reason == 'sensing_stale':
                            h.occupancy.age_s = 2.
                        else:
                            h.rover.age_s = 2.
                        await h.finished()
                        self.assertEqual(h.stops, [reason])
                        self.assertFalse(h.armed)
                        self.assertEqual(h.nav.path, [])
                        self.assertEqual(h.rover.commands[-1], (0., 0.))
                        count = len(h.rover.commands)
                        h.occupancy.age_s = h.rover.age_s = 0.
                        h.occupancy.set(corridor())
                        await asyncio.sleep(.05)
                        self.assertEqual(h.rover.commands[count:], [])


if __name__ == '__main__':
    unittest.main()
