"""Only accepted same-frame RGB-D reaches Explore pacing; real /phone transport."""
import time
import json
import struct
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.tests.test_map_transport import frame, hello, wait_for
from backend.tests.test_pose_freshness import pose


class ScanObservationTests(unittest.TestCase):
    def test_deduplicated_stationary_frames_still_update_evidence_and_reset_discards_it(self):
        with TestClient(create_app(':memory:', capture_directory='')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame())
                wait_for(lambda: client.app.state.scan_observation is not None)
                first = client.app.state.scan_observation
                self.assertTrue(first.usable)
                self.assertEqual(first.pose.position, (1., 2., 3.))
                self.assertEqual(first.pose.session, ('map-session', 1))
                phone.send_bytes(frame(frame_id=2, t_capture=2.))
                wait_for(lambda: client.app.state.map_stats['no_new_points'] == 1)
                second = client.app.state.scan_observation
                self.assertEqual(second.frame_id, 2)
                self.assertEqual(second.pose.captured_at, 2.)
                self.assertEqual(second.keys, first.keys)
                self.assertIs(second.pose.owner, first.pose.owner)
                phone.send_bytes(frame(frame_id=2, t_capture=2.))
                wait_for(lambda: client.app.state.map_stats['discarded_order'] >= 1)
                self.assertIs(client.app.state.scan_observation, second)
                client.post('/session')
                self.assertIsNone(client.app.state.scan_observation)
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame())
                wait_for(lambda: client.app.state.scan_observation is not None)
                self.assertIsNot(client.app.state.scan_observation.pose.owner, first.pose.owner)
            wait_for(lambda: client.app.state.phone is None)
            self.assertIsNone(client.app.state.scan_observation)

    def test_failed_map_commit_and_tracking_loss_do_not_publish_quality_credit(self):
        with TestClient(create_app(':memory:', capture_directory='')) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(hello())
            wait_for(lambda: client.app.state.occupancy is not None)
            with patch.object(client.app.state.occupancy, 'commit', side_effect=RuntimeError('fixture commit refused')):
                phone.send_bytes(frame())
                wait_for(lambda: client.app.state.occupancy_stats['failed'] >= 1)
                self.assertIsNone(client.app.state.scan_observation)
            phone.send_bytes(frame(frame_id=2, t_capture=2.))
            wait_for(lambda: client.app.state.scan_observation is not None)
            phone.send_json(pose(3., tracking='limited'))
            wait_for(lambda: client.app.state.tracking_lost_capture == 3.)
            self.assertIsNone(client.app.state.scan_observation)

    def test_mapping_delay_cannot_rejuvenate_capture_age(self):
        import backend.app as module
        actual = module.capture_observation

        def delayed(*args):
            result = actual(*args)
            time.sleep(.25)
            return result

        with patch('backend.app.capture_observation', delayed), \
                TestClient(create_app(':memory:', capture_directory='')) as client, \
                client.websocket_connect('/phone') as phone:
            phone.send_json(hello())
            sent = time.monotonic()
            payload = frame()
            size = struct.unpack_from('<I', payload)[0]
            header = json.loads(payload[4:4 + size])
            header['t_wall_ms'] = int(time.time() * 1000) - 180
            encoded = json.dumps(header).encode()
            phone.send_bytes(struct.pack('<I', len(encoded)) + encoded + payload[4 + size:])
            wait_for(lambda: client.app.state.scan_observation is not None)
            seen = client.app.state.scan_observation.observed_at
            self.assertLessEqual(seen, sent - .15)
            self.assertGreaterEqual(time.monotonic() - seen, .4)
