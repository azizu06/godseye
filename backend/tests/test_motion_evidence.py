"""Motion occupancy cannot let historical floor votes suppress newly seen hazards."""
import time
import unittest
import numpy as np
from backend.calibration import RoverCalibration
from backend.occupancy import OccupancyGrid
from backend.tests.test_calibration import TEST_CALIBRATION
from backend.tests.test_occupancy import plane


class MotionEvidenceTests(unittest.TestCase):
    def test_new_obstacle_over_long_observed_floor_blocks_motion(self):
        grid = OccupancyGrid(('TEST', 1), calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        floor = plane(-1, 1, -1, 1, -1.2)
        for _ in range(100):
            grid.add(floor, time.monotonic())
        obstacle = np.array([[.025, -.95, .025]])
        for _ in range(3):
            grid.add(obstacle, time.monotonic())
        snapshot = grid.map_snapshot()
        self.assertEqual(snapshot.cell(.025, .025), 2)
        self.assertFalse(snapshot.traversable(.025, .025))

    def test_latest_credible_obstacle_vetoes_motion_before_historical_confirmation(self):
        grid = OccupancyGrid(('TEST', 1), calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        for _ in range(3):
            grid.add(plane(-1, 1, -1, 1, -1.2), time.monotonic())
        grid.add(np.array([[.025, -.95, .025], [.025, -.93, .025], [.025, -.91, .025]]), time.monotonic())
        self.assertFalse(grid.map_snapshot().traversable(.025, .025))

    def test_repeated_frames_elsewhere_do_not_refresh_route_clearance(self):
        grid = OccupancyGrid(('TEST', 1), calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        old = time.monotonic() - 4
        for _ in range(3):
            grid.add(plane(-1, 1, -1, 1, -1.2), old)
        grid.add(plane(.65, .9, .65, .9, -1.2), time.monotonic())
        snapshot = grid.map_snapshot()
        self.assertTrue(snapshot.traversable(0, 0))  # historical plan remains available
        self.assertFalse(snapshot.fresh_clearance(0, 0, time.monotonic(), 1.))

    def test_recent_floor_support_and_empty_frames(self):
        grid = OccupancyGrid(('TEST', 1), calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        for _ in range(3):
            grid.add(plane(-1, 1, -1, 1, -1.2), time.monotonic())
        snapshot = grid.map_snapshot()
        self.assertTrue(snapshot.fresh_clearance(0, 0, time.monotonic(), 1.))
        grid.add(np.empty((0, 3)), time.monotonic() + 2)
        self.assertFalse(grid.map_snapshot().fresh_clearance(0, 0, time.monotonic() + 2, 1.))

class MotionEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unobserved_start_cannot_issue_first_motion(self):
        from dataclasses import replace
        from backend.tests.test_navigator import Harness, Rover, FakeOccupancy, cells, initial_plan
        occupancy = FakeOccupancy(cells())
        original = occupancy.snapshot
        occupancy.snapshot = lambda: replace(original(), free_at=None)
        h = Harness(Rover(.5, .5, 0), occupancy)
        h.nav.start_goal((.5, 2.), initial_plan(occupancy.cells, (.5, .5), (.5, 2.)), h.generation)
        await h.finished()
        self.assertEqual(h.stops, ['sensing_clearance_unknown'])
        self.assertFalse(any(v or w for v, w in h.rover.commands))

    def test_stationary_turn_cannot_reuse_expired_self_occluded_footprint(self):
        grid = OccupancyGrid(('TEST', 1), calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        now = time.monotonic()
        for _ in range(3):
            grid.add(plane(-1, 1, -1, 1, -1.2), now)
        snapshot = grid.map_snapshot()
        self.assertTrue(snapshot.fresh_clearance(0, 0, now, 1.))
        self.assertFalse(snapshot.fresh_clearance(0, 0, now+2, 1.))
        self.assertFalse(snapshot.fresh_clearance(0, .1, now+2, 1.))

class MountedViewTests(unittest.TestCase):
    def test_forward_camera_floor_does_not_certify_blind_start_footprint(self):
        # Raycast floor through an actual perspective depth image, including its
        # near-field blind area. These are TEST camera values, not rover data.
        from backend.mapping import build_point_chunk
        from backend.tests.test_occupancy import floor_frame
        chunk = build_point_chunk(floor_frame(session='TEST'), 'TEST', 1)
        self.assertGreater(len(chunk.positions), 16)
        grid = OccupancyGrid(('TEST', 1), calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        for _ in range(3):
            grid.add(chunk.positions, time.monotonic())
        snapshot = grid.map_snapshot()
        self.assertTrue(snapshot.ready)
        self.assertFalse(snapshot.fresh_clearance(0, 0, time.monotonic(), 1.))

    def test_obstacle_free_frames_cannot_resurrect_motion_hazard(self):
        grid = OccupancyGrid(('TEST', 1), calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        floor = plane(-1, 1, -1, 1, -1.2)
        for _ in range(3):
            grid.add(floor, time.monotonic())
        grid.add(np.array([[.025, -.95, .025]]), time.monotonic())
        for _ in range(20):
            grid.add(floor, time.monotonic())
        self.assertFalse(grid.map_snapshot().traversable(0, 0))
        # Historic display deliberately retains its noise-resistant ratio policy.
        self.assertEqual(grid.snapshot()[2][20, 20], 1)

class CapacityTests(unittest.TestCase):
    def test_full_history_cannot_discard_new_hazard_and_stay_motion_ready(self):
        from backend.occupancy import frame_evidence
        floor = plane(-1, 1, -1, 1, -1.2)
        grid = OccupancyGrid(('TEST', 1), max_voxels=len(frame_evidence(floor).keys),
                             calibration=RoverCalibration.model_validate(TEST_CALIBRATION))
        for _ in range(3):
            grid.add(floor, time.monotonic())
        grid.add(np.concatenate([floor, [[.025, -.95, .025]]]), time.monotonic())
        self.assertIn('motion_evidence_capacity', grid.map_snapshot().blockers)
        self.assertFalse(grid.map_snapshot().traversable(0, 0))

class ReadinessHTTPTests(unittest.TestCase):
    def test_readiness_names_unobserved_motion_envelope(self):
        from types import SimpleNamespace
        from fastapi.testclient import TestClient
        from backend.app import create_app
        from backend.tests.test_rover_relay import KEY, fixture
        from backend.rover_relay import RelayCar
        calibration = RoverCalibration.model_validate(TEST_CALIBRATION)
        grid = OccupancyGrid(('TEST', 1), calibration=calibration)
        for _ in range(3):
            grid.add(plane(-1, 1, -1, 1, -1.2), time.monotonic()-4)
        grid.add(plane(.65, .9, .65, .9, -1.2), time.monotonic())
        with TestClient(create_app(db_path=':memory:', calibration=calibration,
                                  car=RelayCar(KEY, fixture()), capture_directory='')) as client:
            client.app.state.occupancy = grid
            client.app.state.session = ('TEST', 1)
            client.app.state.autonomy_map = grid.map_snapshot()
            client.app.state.pose = SimpleNamespace(transform=[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1], tracking='normal', t_capture=1.)
            client.app.state.pose_at = time.monotonic()
            result = client.get('/autonomy').json()
            client.app.state.session = None
            self.assertIn('sensing_clearance_unknown', result['blockers'])

class ReceiptAgeTests(unittest.TestCase):
    def test_slow_mapping_cannot_rejuvenate_floor_freshness(self):
        from dataclasses import replace
        from fastapi.testclient import TestClient
        from backend.app import create_app
        from backend.mapping import build_point_chunk
        from backend.tests.test_occupancy import floor_frame
        from backend.tests.test_map_transport import hello, wait_for
        floor = plane(-1, 1, -1, 1, 0)
        def slow(payload, session, epoch):
            chunk = build_point_chunk(payload, session, epoch)
            time.sleep(.7)
            return replace(chunk, positions=floor, colors=np.zeros_like(floor))
        with TestClient(create_app(':memory:', build_points=slow,
                calibration=RoverCalibration.model_validate(TEST_CALIBRATION), capture_directory='')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('capture-age'))
                wait_for(lambda: client.app.state.occupancy is not None)
                grid = client.app.state.occupancy
                for _ in range(3):
                    grid.add(floor, time.monotonic()-3)
                packet = floor_frame('capture-age', 1, 1.)
                sent = time.monotonic()
                phone.send_bytes(packet)
                wait_for(lambda: client.app.state.map_stats['published'] >= 1)
                time.sleep(max(0., sent+1.1-time.monotonic()))
                snapshot = grid.map_snapshot()
                self.assertFalse(snapshot.fresh_clearance(0, 0, time.monotonic(), 1.))
                self.assertLessEqual(float(np.max(snapshot.free_at)), sent + .05)

    def test_ingress_capture_age_is_subtracted_and_future_clock_does_not_extend_it(self):
        from dataclasses import replace
        from fastapi.testclient import TestClient
        from backend.app import create_app
        from backend.mapping import build_point_chunk
        from backend.tests.test_occupancy import floor_frame
        from backend.tests.test_map_transport import hello, wait_for, fresh
        floor = plane(-1, 1, -1, 1, 0)
        def measured(payload, session, epoch):
            chunk = build_point_chunk(payload, session, epoch)
            return replace(chunk, positions=floor, colors=np.zeros_like(floor))
        for wall_offset, age in ((-180, .18), (180, 0.)):
            with self.subTest(wall_offset=wall_offset), TestClient(create_app(':memory:', build_points=measured,
                    calibration=RoverCalibration.model_validate(TEST_CALIBRATION), capture_directory='')) as client:
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello('capture-clock'))
                    wait_for(lambda: client.app.state.occupancy is not None)
                    grid = client.app.state.occupancy
                    for _ in range(3):
                        grid.add(floor, time.monotonic()-3)
                    import json, struct
                    raw = floor_frame('capture-clock')
                    size = int.from_bytes(raw[:4], 'little')
                    header = json.loads(raw[4:4+size])
                    header['t_wall_ms'] = int(time.time()*1000)+wall_offset
                    encoded = json.dumps(header).encode()
                    packet = struct.pack('<I', len(encoded))+encoded+raw[4+size:]
                    sent = time.monotonic()
                    phone.send_bytes(packet)
                    wait_for(lambda: client.app.state.map_stats['published'] >= 1)
                    stamped = float(np.max(grid.map_snapshot().free_at))
                    self.assertAlmostEqual(stamped, sent-age, delta=.06)
                    # A replay with a new wall time is still the same sensor capture.
                    phone.send_bytes(fresh(packet))
                    wait_for(lambda: client.app.state.map_stats['discarded_order'] >= 1)
                    self.assertEqual(float(np.max(grid.map_snapshot().free_at)), stamped)
