"""Startup regressions: repeated stale messages and a forward-only mounted camera."""
import tempfile
import time
import unittest
from pathlib import Path
import numpy as np
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.navigator import Navigator, NavSettings, RoverPose
from backend.navigator import planning_grid
from backend.navigation import path_blocked
from backend.occupancy import OccupancyGrid
from backend.prototype import prototype_geometry
from backend.tests.test_map_transport import hello, wait_for
from backend.tests.test_pose_freshness import pose
from backend.tests.test_occupancy import plane

class StartupTests(unittest.TestCase):
    def test_disk_database_uses_wal_without_per_frame_fsync(self):
        with tempfile.TemporaryDirectory() as folder:
            with TestClient(create_app(str(Path(folder) / 'test.db'))) as client:
                async def settings():
                    db=client.app.state.db
                    return db.execute('pragma journal_mode').fetchone()[0], db.execute('pragma synchronous').fetchone()[0]
                self.assertEqual(client.portal.call(settings), ('wal', 1))

    def test_repeated_stale_poses_do_not_restart_stop_handshake(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(hello())
            phone.send_json(pose(1.))
            wait_for(lambda: client.app.state.pose is not None)
            old=pose(2.); old['t_wall_ms']-=1000
            phone.send_json(old)
            wait_for(lambda: client.app.state.stop_reason=='pose_stale')
            generation=client.app.state.motion.generation
            for _ in range(10): phone.send_json(old)
            phone.send_json(pose(3.))
            wait_for(lambda: client.app.state.pose.t_capture==3.)
            self.assertEqual(client.app.state.motion.generation,generation)

    def test_prototype_explores_observed_floor_from_camera_blind_spot(self):
        grid=OccupancyGrid(('test',1),calibration=prototype_geometry(.2286,.127))
        for _ in range(3): grid.add(plane(-1.5,1.5,.25,2.,-1.),time.monotonic())
        nav=Navigator(NavSettings(),pose=lambda:RoverPose(0.,0.,0.,0.,'normal'),
                      occupancy=grid.map_snapshot,submit=lambda *args:True,
                      stop=lambda reason:None,publish=lambda message:None,armed_mode=lambda:'explore')
        result=nav._plan(grid.map_snapshot,(0.,0.),None,True)
        self.assertEqual(result[0],'plan')
        self.assertTrue(result[3].ok,result[3].reason)
        plan_grid, config = planning_grid(result[1], NavSettings())
        self.assertFalse(path_blocked(plan_grid, result[3].points, config),
                         'A* and live route checks must agree about unseen flat floor')
        self.assertTrue(result[1].traversable(0.,0.))
        self.assertEqual(result[1].cell(*result[2]),1,'Explore must choose observed floor')
        # A real obstacle in the same blind spot still prevents departure.
        for _ in range(3): grid.add(plane(-.1,.1,-.1,.1,-.7),time.monotonic())
        blocked=nav._plan(grid.map_snapshot,(0.,0.),None,True)
        self.assertEqual(blocked[3].reason,'start_blocked')
