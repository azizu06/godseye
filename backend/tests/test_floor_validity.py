"""Floor readiness and obstacle geometry; synthetic inputs, no hardware."""
import time
import unittest

import numpy as np

from backend.calibration import RoverCalibration
from backend.navigator import map_problem
from backend.occupancy import OccupancyGrid, frame_evidence
from backend.prototype import prototype_geometry
from backend.tests.test_occupancy import plane
from tools.car_rehearsal import TEST_CALIBRATION


# The actual binary /phone route shares its existing age/session gates. Floor
# anchors supplement sparse floor depth; they do not replace obstacle sensing.
from backend.frame_bundle import parse_frame_bundle
from backend.occupancy import FREE, OCCUPIED
from backend.tests.test_map_transport import fresh
from backend.tests.test_pose_freshness import assert_rejected, with_wall_time
from backend.tests.test_prototype_depth import phone_link, send, SESSION
from tools.fake_phone import frame_bundle, look_pose, world_sensors

def profiles():
    return (RoverCalibration.model_validate(TEST_CALIBRATION), prototype_geometry(.2286, .127))


class FloorValidityTests(unittest.TestCase):
    def test_ceiling_dominant_depth_cannot_authorize_motion_or_publish_a_floor(self):
        # Same heights as the preserved real scene, with no guessed mount height.
        points = plane(-1., 1., -1., 1., 1.13)
        for calibration in profiles():
            with self.subTest(prototype=getattr(calibration, 'unknown_traversable', False)):
                grid = OccupancyGrid(('floor-test', 1), calibration=calibration)
                for _ in range(3):
                    grid.commit(frame_evidence(points, camera_y=-1.07, camera_xz=(0., 0.)), time.monotonic())
                snapshot = grid.map_snapshot()
                self.assertIn('no_floor', snapshot.blockers)
                self.assertIsNone(snapshot.floor_y)
                self.assertFalse(snapshot.traversable(0., 0.))
                self.assertEqual(map_problem(snapshot, 1.), 'no_floor')
                self.assertIsNone(grid.message_if_due(time.monotonic()))

    def test_a_retained_depth_floor_above_current_camera_is_unavailable(self):
        for calibration in profiles():
            with self.subTest(prototype=getattr(calibration, 'unknown_traversable', False)):
                grid = OccupancyGrid(('floor-test', 1), calibration=calibration)
                points = plane(-1., 1., -1., 1., -1.2)
                for _ in range(3):
                    grid.commit(frame_evidence(points, camera_y=0.), time.monotonic())
                self.assertTrue(grid.map_snapshot().ready)
                grid.commit(frame_evidence(points, camera_y=-1.3), time.monotonic())
                self.assertIn('no_floor', grid.map_snapshot().blockers)


FLOOR = dict(y=-1.2, polygon=[[-1., -1.], [1., -1.], [1., 3.], [-1., 3.]])


def anchored_packet(frame_id, *, anchor=True, confidence=2, floor=FLOOR):
    # Phone 13 cm above a ray-cast synthetic floor, looking horizontally at a
    # 50 cm box. Glossy-floor pixels have zero confidence; box depth stays real.
    pose = look_pose(frame_id, float(frame_id), time.time_ns() // 1_000_000,
                     SESSION, position=(0., -1.07, 0.), pitch=0.)
    jpeg, depth, original = world_sensors(pose['transform'])
    values = np.frombuffer(depth, dtype='<f4').reshape(192, 256)
    conf = np.frombuffer(original, dtype='u1').copy().reshape(values.shape)
    rows = np.arange(192)[:, None]
    world_y = -1.07 - ((rows + .5) * 720 / 192 - 360) * values / 720
    conf[np.abs(world_y + 1.2) < .01] = 0
    conf[conf > 0] = confidence
    payload = frame_bundle(pose, (jpeg, depth, conf.tobytes()))
    return fresh(payload, version=2, floor=floor) if anchor else payload


class PhoneFloorTests(unittest.TestCase):
    def test_sparse_floor_anchor_seeds_free_cells_and_box_depth_remains_occupied(self):
        for prototype, confidence in ((False, 2), (True, 1)):
            with self.subTest(prototype=prototype), phone_link(prototype) as (client, phone):
                for frame_id in (5, 10, 15):
                    send(client, phone, anchored_packet(frame_id, confidence=confidence))
                state = client.app.state
                snapshot = state.occupancy.map_snapshot()
                self.assertEqual(snapshot.blockers, ())
                self.assertEqual(snapshot.floor_y, -1.2)
                self.assertEqual(snapshot.cell(0., .5), FREE)
                self.assertEqual(snapshot.cell(.4, 1.9), OCCUPIED)
                self.assertFalse(snapshot.traversable(.4, 1.9))
                wire = state.occupancy.message_if_due(time.monotonic() + 2.) or state.occupancy.last_message
                self.assertEqual(wire['floor_y'], -1.2)
                self.assertEqual(wire['session_id'], SESSION)
                self.assertEqual(state.map_stats['rejected'], 0)
                self.assertEqual(state.chunk_id > 0, not prototype)
                self.assertFalse(client.get('/health').json()['armed'])

    def test_floor_metadata_without_reliable_obstacle_depth_cannot_refresh_sensing(self):
        for prototype in (False, True):
            with self.subTest(prototype=prototype), phone_link(prototype) as (client, phone):
                send(client, phone, anchored_packet(5, confidence=0))
                snapshot = client.app.state.occupancy.map_snapshot()
                self.assertIsNone(snapshot.accepted_at)
                self.assertIn('no_floor', snapshot.blockers)
                self.assertFalse(snapshot.traversable(0., .5))
                self.assertFalse(client.get('/health').json()['armed'])

    def test_stale_replayed_untracked_and_implausible_anchors_do_not_refresh_sensing(self):
        for prototype in (False, True):
            with self.subTest(prototype=prototype), phone_link(prototype) as (client, phone):
                send(client, phone, anchored_packet(5))
                grid = client.app.state.occupancy
                stamp, revision = grid.accepted_at, grid.revision
                bad = [anchored_packet(5), with_wall_time(anchored_packet(10), 0),
                       fresh(anchored_packet(15), tracking='limited'),
                       anchored_packet(20, floor={**FLOOR, 'y': 1.13}),
                       anchored_packet(25, floor={**FLOOR, 'y': -3.})]
                for payload in bad:
                    send(client, phone, payload)
                    self.assertEqual((grid.accepted_at, grid.revision), (stamp, revision))
                self.assertFalse(client.get('/health').json()['armed'])

    def test_other_session_or_epoch_anchor_never_commits(self):
        for identity in ({'session_id': 'other'}, {'map_epoch': 2}):
            with self.subTest(identity=identity), phone_link() as (client, phone):
                send(client, phone, anchored_packet(5))
                grid = client.app.state.occupancy
                stamp, revision = grid.accepted_at, grid.revision
                phone.send_bytes(fresh(anchored_packet(10), **identity))
                assert_rejected(self, client, phone)
                self.assertEqual((grid.accepted_at, grid.revision), (stamp, revision))
                self.assertFalse(client.get('/health').json()['armed'])

    def test_new_session_does_not_inherit_classified_floor(self):
        with phone_link() as (client, phone):
            for frame_id in (5, 10, 15):
                send(client, phone, anchored_packet(frame_id))
            self.assertTrue(client.app.state.occupancy.map_snapshot().ready)
            previous = client.app.state.occupancy
            reset = client.post('/session')
            self.assertEqual(reset.status_code, 200)
            current = client.app.state.occupancy
            self.assertIsNot(current, previous)
            self.assertIsNone(current.map_snapshot().floor_y)
            self.assertIn('no_floor', current.map_snapshot().blockers)
            from backend.tests.test_map_transport import hello
            with client.websocket_connect('/phone') as next_phone:
                next_phone.send_json(hello(reset.json()['session_id']))
                for frame_id in (5, 10, 15):
                    send(client, next_phone, fresh(anchored_packet(frame_id, anchor=False),
                                                  session_id=reset.json()['session_id']))
                self.assertIn('no_floor', current.map_snapshot().blockers)


class InvalidFloorExploreTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_explore_never_dispatches_motion_on_a_ceiling_floor(self):
        from backend.navigator import Navigator, NavSettings, RoverPose
        from backend.tests.test_navigator import wait_until
        for calibration in profiles():
            with self.subTest(prototype=getattr(calibration, 'unknown_traversable', False)):
                grid = OccupancyGrid(('floor-test', 1), calibration=calibration)
                for _ in range(3):
                    grid.commit(frame_evidence(plane(-1., 1., -1., 1., 1.13), camera_y=-1.07),
                                time.monotonic())
                commands, stops = [], []
                def submit(generation, mode, v, w):
                    commands.append((v, w))
                    return True
                def stop(reason):
                    stops.append(reason)
                    commands.append((0., 0.))
                    nav.halt()
                nav = Navigator(NavSettings(rate_hz=200.),
                                pose=lambda: RoverPose(0., 0., 0., 0., 'normal'),
                                occupancy=grid.map_snapshot, submit=submit, stop=stop,
                                publish=lambda message: None, armed_mode=lambda: 'explore')
                try:
                    nav.start_explore(1)
                    await wait_until(lambda: bool(stops))
                    self.assertEqual(stops, ['no_floor'])
                    self.assertTrue(commands)
                    self.assertTrue(all(command == (0., 0.) for command in commands), commands)
                    self.assertEqual(nav.path, [])
                finally:
                    await nav.aclose()


class AnchorExtentTests(unittest.TestCase):
    def test_remote_floor_cannot_set_local_rover_floor(self):
        from backend.frame_bundle import FrameValidationError
        remote = dict(y=-1.2, polygon=[[100., 100.], [101., 100.], [101., 101.], [100., 101.]])
        with self.assertRaises(FrameValidationError):
            parse_frame_bundle(anchored_packet(5, floor=remote), session_id=SESSION, map_epoch=1)
