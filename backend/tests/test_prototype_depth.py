"""Binary /phone depth acceptance; real parsing/mapping/occupancy, no hardware."""
from contextlib import contextmanager
import time
import unittest
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from backend.app import Listener, create_app
from backend.calibration import RoverCalibration
from backend.navigator import map_problem
from backend.prototype import prototype_geometry
from backend.tests.test_map_transport import fresh, hello, wait_for
from backend.tests.test_pose_freshness import assert_rejected, with_wall_time
from tools.car_rehearsal import TEST_CALIBRATION
from tools.fake_phone import frame_bundle, look_pose, world_sensors


SESSION = 'prototype-depth'


@contextmanager
def phone_link(prototype=True):
    calibration = prototype_geometry(.2286, .127) if prototype else RoverCalibration.model_validate(TEST_CALIBRATION)
    with TestClient(create_app(':memory:', calibration=calibration, capture_directory='')) as client, \
            client.websocket_connect('/phone') as phone:
        phone.send_json(hello(SESSION))
        wait_for(lambda: client.app.state.session == (SESSION, 1))
        yield client, phone


def packet(frame_id, confidence=1, *, sample_count=None, high_points=0, depth_value=None):
    pose = look_pose(frame_id, float(frame_id), time.time_ns() // 1_000_000, SESSION, pitch=.9)
    jpeg, depth, original = world_sensors(pose['transform'])
    conf = np.frombuffer(original, dtype='u1').copy()
    indices = np.flatnonzero(conf == 2)
    conf[conf == 2] = confidence
    if sample_count is not None:
        conf[:] = 0
        conf[indices[:sample_count]] = confidence
    conf[indices[:high_points]] = 2
    if depth_value is not None:
        values = np.frombuffer(depth, dtype='<f4').copy()
        values[:] = depth_value
        depth = values.tobytes()
    return frame_bundle(pose, (jpeg, depth, conf.tobytes()))


def send(client, phone, payload):
    stats = client.app.state.map_stats
    outcomes = ('published', 'no_new_points', 'rejected', 'failed', 'discarded_stale',
                'discarded_order', 'discarded_wall_time', 'discarded_tracking')
    before = sum(stats[k] for k in outcomes)
    phone.send_bytes(payload)
    wait_for(lambda: sum(stats[k] for k in outcomes) > before)


class PrototypeDepthTests(unittest.TestCase):
    def test_medium_only_frames_accumulate_navigation_floor_without_display_points(self):
        with phone_link() as (client, phone):
            for frame_id in range(1, 4):
                send(client, phone, packet(frame_id))
            state = client.app.state
            snapshot = state.occupancy.map_snapshot()
            self.assertEqual(state.occupancy.revision, 3, dict(state.map_stats))
            self.assertGreater(state.occupancy.voxels, 0)
            self.assertEqual(snapshot.blockers, ())
            self.assertAlmostEqual(snapshot.floor_y, -1.2, delta=.03)
            self.assertIsNotNone(snapshot.accepted_at)
            self.assertEqual(snapshot.sensing_confidence, .5)
            self.assertEqual(state.chunk_id, 0)
            self.assertEqual(state.map_stats['no_new_points'], 3)
            self.assertFalse(client.get('/health').json()['armed'])

    def test_measured_policy_rejects_medium_and_both_profiles_keep_high_confidence_success(self):
        for prototype, confidence in ((False, 1), (False, 2), (True, 2)):
            with self.subTest(prototype=prototype, confidence=confidence), phone_link(prototype) as (client, phone):
                for frame_id in range(1, 4):
                    send(client, phone, packet(frame_id, confidence))
                state = client.app.state
                accepted = confidence == 2
                self.assertEqual(state.occupancy.revision, 3 if accepted else 0)
                self.assertEqual(state.chunk_id > 0, accepted)
                self.assertEqual(state.map_stats['rejected'], 0 if accepted else 3)
                snapshot = state.occupancy.map_snapshot()
                if accepted:
                    self.assertEqual(snapshot.blockers, ())
                    self.assertEqual(snapshot.sensing_confidence, 1.)
                else:
                    self.assertIsNone(snapshot.accepted_at)
                    self.assertIn('no_floor', snapshot.blockers)
                self.assertFalse(client.get('/health').json()['armed'])

    def test_medium_sample_minimum_and_sparse_high_display_policy_stay_separate(self):
        for count, high, accepted, displayed in ((15, 0, False, False), (16, 0, True, False),
                                                 (16, 15, True, False), (32, 16, True, True)):
            with self.subTest(count=count, high=high), phone_link() as (client, phone):
                listener = Listener()
                client.app.state.listeners.add(listener)
                send(client, phone, packet(1, sample_count=count, high_points=high))
                state = client.app.state
                self.assertEqual(state.occupancy.revision, int(accepted))
                self.assertEqual(state.chunk_id, int(displayed))
                if displayed:
                    # Display remains high-only; medium points are not promoted.
                    self.assertLessEqual(len(listener.points[0]['positions']) // 3, high)
                else:
                    self.assertEqual(len(listener.points), 0)
                if accepted:
                    self.assertIsNotNone(state.occupancy.accepted_at)
                    self.assertGreater(state.occupancy.voxels, 0)
                    self.assertIn('no_floor', state.occupancy.map_snapshot().blockers,
                                  'sparse accepted depth is not a motion-ready floor')
                else:
                    self.assertIsNone(state.occupancy.accepted_at)
                self.assertFalse(client.get('/health').json()['armed'])

    def test_low_empty_invalid_and_malformed_depth_do_not_refresh_accepted_sensing(self):
        cases = [('low', dict(confidence=0)), ('empty', dict(sample_count=0)),
                 ('nan', dict(depth_value=np.nan)), ('infinite', dict(depth_value=np.inf)),
                 ('zero', dict(depth_value=0.)), ('far', dict(depth_value=30.)),
                 ('malformed_confidence', dict(confidence=3))]
        with phone_link() as (client, phone):
            send(client, phone, packet(1, 2))
            send(client, phone, packet(2))
            state = client.app.state
            stamp, revision, chunk_id = state.occupancy.accepted_at, state.occupancy.revision, state.chunk_id
            for frame_id, (name, changes) in enumerate(cases, 3):
                with self.subTest(name=name):
                    send(client, phone, packet(frame_id, **changes))
                    self.assertEqual((state.occupancy.accepted_at, state.occupancy.revision, state.chunk_id),
                                     (stamp, revision, chunk_id))
                    self.assertEqual(state.map_stats['failed'], 0)
                    self.assertFalse(client.get('/health').json()['armed'])
            self.assertEqual(state.map_stats['rejected'], len(cases))

    def test_high_to_medium_transition_keeps_floor_fresh_but_replays_old_and_untracked_data_do_not(self):
        # These are replay/tracking cases, not an encoder-speed benchmark. Four
        # prebuilt RGB-D packets can otherwise age the first replay past 250 ms
        # and correctly hit wall-age rejection before the replay gate. Monotonic
        # sensing age below remains real, including the deliberate stale wait.
        wall = time.time()
        with patch('time.time', return_value=wall), \
                patch('time.time_ns', return_value=int(wall * 1e9)), phone_link() as (client, phone):
            send(client, phone, packet(1, 2))
            first = client.app.state.occupancy.accepted_at
            send(client, phone, packet(2))
            send(client, phone, packet(3))
            state = client.app.state
            stamp = state.occupancy.accepted_at
            self.assertGreater(stamp, first)
            self.assertEqual(state.occupancy.revision, 3)
            self.assertEqual(state.chunk_id, 1)
            for payload in (packet(3), packet(2), with_wall_time(packet(4), 0),
                            fresh(packet(5), tracking='limited')):
                send(client, phone, payload)
                self.assertEqual((state.occupancy.accepted_at, state.occupancy.revision), (stamp, 3))
            self.assertEqual(state.map_stats['discarded_order'], 2)
            self.assertEqual(state.map_stats['discarded_wall_time'], 1)
            self.assertEqual(state.map_stats['discarded_tracking'], 1)
            # Still reject actual stale accepted sensing; receipt of invalid data
            # and pose updates must not refresh this clock.
            time.sleep(1.05)
            self.assertEqual(map_problem(state.occupancy.map_snapshot(), 1.), 'sensing_stale')
            send(client, phone, packet(6))
            self.assertGreater(state.occupancy.accepted_at, stamp)
            self.assertIsNone(map_problem(state.occupancy.map_snapshot(), 1.))
            self.assertFalse(client.get('/health').json()['armed'])

    def test_medium_depth_from_another_session_or_epoch_is_rejected_before_commit(self):
        for identity in ({'session_id': 'other'}, {'map_epoch': 2}):
            with self.subTest(identity=identity), phone_link() as (client, phone):
                send(client, phone, packet(1))
                grid = client.app.state.occupancy
                stamp = grid.accepted_at
                phone.send_bytes(fresh(packet(2), **identity))
                assert_rejected(self, client, phone)
                self.assertEqual((grid.revision, grid.accepted_at), (1, stamp))
                self.assertFalse(client.get('/health').json()['armed'])


if __name__ == '__main__':
    unittest.main()
