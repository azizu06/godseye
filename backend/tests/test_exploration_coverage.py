"""Sustained real Explore runner, fake pose/map/clock/plant only."""
import unittest
import numpy as np
from backend.tests.test_detour_following import exploring, advance


def multi_region_room():
    cells = np.ones((100, 100), np.uint8)
    cells[:3] = cells[-3:] = cells[:, :3] = cells[:, -3:] = 2
    cells[85:97, 10:90] = 0
    cells[10:90, 85:97] = 0
    cells[10:35, 3:15] = 0
    cells[45:55, 45:55] = 2
    return cells


class CoverageTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_frontiers_do_not_trap_explore_on_one_room_edge(self):
        async with exploring(multi_region_room(), start=(2.5, 1., 0.)) as (h, clock):
            await advance(h, clock, 180.)
            unique = {(round(x / .25), round(z / .25)) for x, z in h.rover.trace}
            self.assertTrue(any(x < 1. for x, z in h.rover.trace), 'must reach the remaining west frontier')
            self.assertGreater(len(unique), 35)
            self.assertEqual(h.stops, [])
            self.assertTrue(all(h.occupancy.snapshot().traversable(x, z) for x, z in h.rover.trace))

    async def test_crossing_obstacle_waits_then_resumes_after_clearance_evidence(self):
        cells = np.ones((120, 60), np.uint8)
        cells[:3] = cells[:, :3] = cells[:, -3:] = 2
        cells[100:, 3:-3] = 0
        async with exploring(cells, start=(1.5, .8, 0.)) as (h, clock):
            await advance(h, clock, 4.)
            before = h.rover.z
            blocked = cells.copy()
            blocked[40:48, :] = 2  # crossing object now blocks every forward corridor
            h.occupancy.set(blocked)
            await advance(h, clock, 8.)
            self.assertEqual(h.stops, [])
            self.assertTrue(h.nav.active)
            self.assertEqual(h.rover.commands[-1], (0., 0.))
            self.assertLess(h.rover.z, 2.)
            self.assertGreaterEqual(h.rover.z, before)
            h.occupancy.set(cells)  # fresh accepted depth clears the object
            await advance(h, clock, 30., until=lambda: h.rover.z > 3.)
            self.assertGreater(h.rover.z, 3.)
            self.assertEqual(h.stops, [])

    async def test_localized_off_path_object_keeps_forward_progress(self):
        cells = np.ones((120, 100), np.uint8)
        cells[:3] = cells[:, :3] = cells[:, -3:] = 2
        cells[100:, 3:-3] = 0
        cells[25:35, 7:17] = 2  # object well outside intended swept corridor
        async with exploring(cells, start=(2.5, .8, 0.)) as (h, clock):
            await advance(h, clock, 15., until=lambda: h.rover.z > 2.8)
            self.assertGreater(h.rover.z, 2.8)
            self.assertEqual(h.stops, [])
            self.assertTrue(all(h.occupancy.snapshot().traversable(x, z) for x, z in h.rover.trace))


class ExplorationMemoryTests(unittest.TestCase):
    def test_necessary_return_remains_traversable_and_failures_need_changed_geometry(self):
        from dataclasses import replace
        from backend.exploration import ExplorationMemory
        from backend.navigation import Grid
        from backend.tests.test_navigator import FakeOccupancy
        memory = ExplorationMemory()
        snapshot = FakeOccupancy(np.ones((80, 80), np.uint8)).snapshot()
        grid = Grid.from_array(snapshot.cells, origin=snapshot.origin, cell_m=snapshot.cell_m)
        memory.observe(snapshot.session, 1., 1., 0.)
        memory.targets(snapshot, grid)
        memory.reject(snapshot.session, (2., 2.))
        mask = memory.targets(replace(snapshot, revision=100), grid)
        self.assertFalse(mask[grid.cell_of(1., 1.)])
        self.assertFalse(mask[grid.cell_of(2., 2.)])
        self.assertTrue(snapshot.traversable(1., 1.), 'memory never paints visited floor occupied')
        changed = snapshot.cells.copy()
        changed[5, 5] = 2
        changed_grid = Grid.from_array(changed, origin=snapshot.origin, cell_m=snapshot.cell_m)
        self.assertTrue(memory.targets(snapshot, changed_grid)[grid.cell_of(2., 2.)])
        memory.observe(('NEXT', 1), 3., 3., 0.)
        memory.targets(snapshot, grid)  # cancelled old worker must not reset the new session
        self.assertEqual(memory.stats()['session'], ('NEXT', 1))
        self.assertEqual(memory.stats()['visited_cells'], 1)

    def test_repeat_closed_lap_without_novel_cells_requests_replan(self):
        from backend.exploration import ExplorationMemory
        memory = ExplorationMemory()
        lap = [(1., 1., 0.), (2., 1., 1.57), (2., 2., 3.14), (1., 2., -1.57), (1., 1., 0.)]
        self.assertFalse(any(memory.observe(('TEST', 1), *pose) for pose in lap))
        self.assertTrue(any(memory.observe(('TEST', 1), *pose) for pose in lap))
        self.assertGreater(memory.stats()['loop_replans'], 0)

    def test_adaptive_nominal_command_responds_to_clearance_unknown_quality_and_age(self):
        from dataclasses import replace
        from backend.navigator import exploration_command
        from backend.tests.test_navigator import FakeOccupancy
        snapshot = FakeOccupancy(np.ones((100, 100), np.uint8)).snapshot()
        now = snapshot.accepted_at
        clear = exploration_command(snapshot, 2., 2., 0., .2, .1, now, 1.)
        low_quality = exploration_command(replace(snapshot, sensing_confidence=.5), 2., 2., 0., .2, .1, now, 1.)
        aged = exploration_command(snapshot, 2., 2., 0., .2, .1, now+.8, 1.)
        unknown = snapshot.cells.copy(); unknown[44:60, 40] = 0
        unseen = exploration_command(replace(snapshot, cells=unknown), 2., 2., 0., .2, .1, now, 1.)
        obstacle = snapshot.cells.copy(); obstacle[43:45, 45:47] = 2
        narrow = exploration_command(replace(snapshot, cells=obstacle), 2., 2., 0., .2, .1, now, 1.)
        for reduced in (low_quality, aged, unseen, narrow):
            self.assertLess(reduced[0], clear[0])
            self.assertAlmostEqual(reduced[1]/reduced[0], clear[1]/clear[0])
        self.assertEqual(exploration_command(snapshot, 2., 2., 0., 0., 0., now, 1.), (0., 0.))

    def test_frontier_memory_mask_survives_prototype_off_grid_fit(self):
        from backend.navigation import Grid, PlannerConfig, nearest_frontier, preferred_explore_frontier
        grid = Grid.from_array(np.ones((30, 30), np.uint8), origin=(0., 0.), cell_m=.05)
        config = PlannerConfig(unknown_traversable=True)
        mask = np.ones(grid.cells.shape, bool)
        mask[5:15, 5:15] = False
        for select in (lambda: nearest_frontier(grid, (-.1, .5), config, allow_unknown=True, target_mask=mask),
                       lambda: preferred_explore_frontier(grid, (-.1, .5), 0., config, allow_unknown=True, target_mask=mask)):
            goal = select()
            self.assertTrue(goal is None or all(np.isfinite(goal)))
