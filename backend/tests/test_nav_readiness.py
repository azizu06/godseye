"""Navigation must consume motion-ready snapshots, never an unmapped fallback."""
import time
import unittest

import numpy as np

from backend.calibration import RoverCalibration
from backend.navigation import Grid, PlannerConfig, plan_path
from backend.navigator import Navigator, NavSettings, RoverPose
from backend.occupancy import OccupancyGrid
from backend.tests.test_calibration import TEST_CALIBRATION
from backend.tests.test_occupancy import plane

def feed(grid, positions):
    for _ in range(3):
        grid.add(positions, time.monotonic())


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    def nav(self, grid):
        return Navigator(NavSettings(), pose=lambda: RoverPose(0., 0., 0., 0., 'normal'),
                         occupancy=grid.map_snapshot, submit=lambda *args: True,
                         stop=lambda reason: None, publish=lambda message: None,
                         armed_mode=lambda: 'navigate')

    async def test_goal_refuses_uncalibrated_floor(self):
        grid = OccupancyGrid(('test', 1))
        feed(grid, plane(-1, 1, -1, 1, -1.2))
        result = await self.nav(grid).plan_once((0., .5))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'calibration_missing')

    async def test_unverified_and_unsensed_maps_refuse_goal_and_explore(self):
        for change, reason in [({'measured_by': None}, 'calibration_unverified'), ({}, 'no_floor')]:
            grid = OccupancyGrid(('TEST', 1), calibration=RoverCalibration.model_validate({**TEST_CALIBRATION, **change}))
            if change:
                feed(grid, plane(-1, 1, -1, 1, -1.2))
            nav = self.nav(grid)
            for explore in (False, True):
                result = nav._plan(grid.map_snapshot, (0., 0.), (0., .5), explore)[3]
                self.assertEqual(result.reason, reason)

    async def test_fresh_pose_does_not_refresh_stale_sensing(self):
        grid = OccupancyGrid(('TEST', 1), calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        for _ in range(3):
            grid.add(plane(-1, 1, -1, 1, -1.2), time.monotonic() - 2.)
        self.assertEqual((await self.nav(grid).plan_once((0., .5))).reason, 'sensing_stale')

    async def test_unknown_off_grid_and_footprint_clearance_are_refused(self):
        grid = OccupancyGrid(('TEST', 1), calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        feed(grid, plane(-1, 1, -1, 1, -1.2))
        nav = self.nav(grid)
        self.assertTrue((await nav.plan_once((0., .5))).ok)
        self.assertEqual((await nav.plan_once((5., 5.))).reason, 'out_of_bounds')
        self.assertFalse((await nav.plan_once((.9, .9))).ok)

    def test_narrow_corridor_and_unknown_neighbors_block_entire_path_cells(self):
        cells = np.ones((40, 40), np.uint8)
        cells[:, :14] = cells[:, 26:] = 2  # 0.60 m corridor; TEST footprint needs >0.557 m
        grid = Grid.from_array(cells, origin=(-1., -1.), cell_m=.05)
        cfg = PlannerConfig(robot_radius_m=.2785, margin_m=0., unknown_traversable=False,
                            footprint_clearance=True, snap_radius_m=0., start_snap_radius_m=0.)
        # Whole cell clearance conservatively refuses this corridor: cell edges are too near.
        self.assertFalse(plan_path(grid, (.025, -.5), (.025, .5), cfg).ok)
        cells[:, 13:27] = 1  # 0.70 m; enough room even throughout the center cell
        wide = Grid.from_array(cells, origin=(-1., -1.), cell_m=.05)
        self.assertTrue(plan_path(wide, (.025, -.5), (.025, .5), cfg).ok)
        cells[20, 20] = 0
        unknown = Grid.from_array(cells, origin=(-1., -1.), cell_m=.05)
        self.assertFalse(plan_path(unknown, (.025, -.5), (.025, .5), cfg).ok)


class LiveSensingTests(unittest.TestCase):
    def test_sensing_loss_stops_navigation_even_with_fresh_pose_and_detector(self):
        from tools.car_rehearsal import scene, arm, wait_for
        with scene(map_max_age_s=.6, no_progress_s=5.) as (client, car, source):
            arm(client)
            self.assertEqual(client.post('/goal', json={'x': 0., 'z': .5}).status_code, 200)
            wait_for(lambda: any(call[0] == 'send' and call[1] for call in car.calls))
            source.frames = False
            wait_for(lambda: not client.app.state.armed)
            self.assertEqual(client.app.state.stop_reason, 'sensing_stale')
            self.assertEqual(car.calls[-1], ('zero',))
            self.assertEqual(client.app.state.nav.path, [])
            self.assertEqual(client.get('/health').json()['phone'], 'ok')
