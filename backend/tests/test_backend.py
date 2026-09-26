import unittest
from fastapi.testclient import TestClient
from backend.app import create_app


class BackendTests(unittest.TestCase):
    def test_starts_disarmed_and_stop_always_accepted(self):
        with TestClient(create_app(':memory:')) as client:
            self.assertFalse(client.get('/health').json()['armed'])
            self.assertEqual(client.post('/arm').status_code, 409)
            self.assertEqual(client.post('/stop').status_code, 200)
            self.assertEqual(client.get('/health').json()['stop_reason'], 'operator_stop')
            self.assertEqual(client.post('/manual', json={'v_mps': .1, 'yaw_rate_rps': 0}).status_code, 409)

    def hello(self, epoch=1):
        return dict(version=1, type='hello', device='test-phone', session_id='test-session',
                    map_epoch=epoch, supports_scene_depth=True, supports_mesh=True)

    def pose(self, **overrides):
        import time
        result = dict(version=1, type='pose', session_id='test-session', map_epoch=1,
                      frame_id=1, t_capture=1.0, t_wall_ms=int(time.time()*1000),
                      transform=[1.,0.,0.,0., 0.,1.,0.,0., 0.,0.,1.,0., 2.,3.,4.,1.], tracking='normal')
        result.update(overrides)
        return result

    def next_type(self, ws, wanted):
        for _ in range(20):
            message = ws.receive_json()
            self.assertEqual(message['version'], 1)
            if message['type'] == wanted:
                return message
        self.fail('Missing live message: ' + wanted)

    def test_phone_pose_reaches_live_and_tracking_loss_latches(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            self.next_type(live, 'health')
            with client.websocket_connect('/phone') as phone:
                phone.send_json(self.hello())
                phone.send_json(self.pose())
                self.assertEqual(self.next_type(live, 'pose')['position'], [2.,3.,4.])
                phone.send_json(self.pose(t_capture=2.0, tracking='limited'))
                health = self.next_type(live, 'health')
                self.assertEqual(health['phone'], 'stale')
                self.assertFalse(health['armed'])
                # Watchdog may supersede tracking_lost after 250 ms, both are stops.
                self.assertIn(health['stop_reason'], ['tracking_lost', 'pose_stale'])

    def test_epoch_mismatch_is_closed_and_disconnect_disarms(self):
        from starlette.websockets import WebSocketDisconnect
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(self.hello())
                phone.send_json(self.pose(map_epoch=2))
                with self.assertRaises(WebSocketDisconnect) as error:
                    phone.receive_json()
                self.assertEqual(error.exception.code, 1008)
            self.assertFalse(client.get('/health').json()['armed'])

    def bundle(self):
        import json
        header = self.pose(type='frame')
        header.update(image=dict(width=1, height=1, jpeg_len=3, intrinsics=[1.,0.,0.,0.,1.,0.,0.,0.,1.], orientation='landscape_right'),
                      depth=dict(width=1, height=1, format='float32_m', len=4),
                      confidence=dict(width=1, height=1, format='uint8_0_2', len=1))
        encoded = json.dumps(header).encode()
        return len(encoded).to_bytes(4, 'little') + encoded + b'jpg' + b'\x00\x00\x80\x3f' + b'\x02'

    def test_binary_frame_same_capture_pose_reaches_live(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(self.hello())
                phone.send_bytes(self.bundle())
                self.assertEqual(self.next_type(live, 'pose')['position'], [2.,3.,4.])

    def test_truncated_binary_frame_is_rejected(self):
        from starlette.websockets import WebSocketDisconnect
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            phone.send_bytes(self.bundle()[:-1])
            with self.assertRaises(WebSocketDisconnect) as error:
                phone.receive_json()
            self.assertEqual(error.exception.code, 1008)

    def test_bad_version_and_pose_before_hello_are_rejected(self):
        from starlette.websockets import WebSocketDisconnect
        for message in [self.pose(), dict(self.hello(), version=2)]:
            with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
                phone.send_json(message)
                with self.assertRaises(WebSocketDisconnect):
                    phone.receive_json()

    def test_rest_modes_limits_and_explicit_stubs(self):
        with TestClient(create_app(':memory:')) as client:
            self.assertEqual(client.post('/mode', json={'mode': 'explore'}).json()['mode'], 'explore')
            self.assertFalse(client.get('/health').json()['armed'])
            self.assertEqual(client.post('/manual', json={'v_mps': 1., 'yaw_rate_rps': 0.}).status_code, 422)
            self.assertEqual(client.post('/goal', json={'x': 1., 'z': 2.}).status_code, 409)  # disarmed
            self.assertEqual(client.post('/rescan').status_code, 409)  # no map to rescan yet
            self.assertEqual(client.post('/ask', json={'question': 'backpack?'}).status_code, 200)
            session = client.post('/session').json()
            self.assertEqual(session['version'], 1)
            self.assertEqual(client.get('/objects').json()['objects'], [])
            self.assertEqual(client.get('/events').json()['events'], [])

    def test_stale_pose_does_not_report_phone_ok(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(self.hello())
                phone.send_json(self.pose(t_wall_ms=0))
                health = self.next_type(live, 'health')
                while health['phone'] == 'down':
                    health = self.next_type(live, 'health')
                self.assertEqual(health['phone'], 'stale')
                self.assertFalse(health['armed'])

    def test_drive_adapter_only_logs(self):
        from backend.drive import drive
        with self.assertLogs('backend.drive', level='INFO') as logs:
            drive(.1, .2)
        self.assertIn('not transmitted', logs.output[0])

    def test_second_phone_cannot_replace_owner(self):
        from starlette.websockets import WebSocketDisconnect
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as first:
            first.send_json(self.hello())
            first.send_json(self.pose())
            with client.websocket_connect('/phone') as second:
                second.send_json(self.hello(epoch=2))
                with self.assertRaises(WebSocketDisconnect):
                    second.receive_json()
            self.assertEqual(client.get('/health').json()['phone'], 'ok')

    def test_new_session_revokes_previous_phone(self):
        from starlette.websockets import WebSocketDisconnect
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            phone.send_json(self.pose())
            # Live delivery serves as a barrier that hello/pose have been processed.
            import time
            deadline = time.monotonic() + 1
            while client.get('/health').json()['phone'] != 'ok' and time.monotonic() < deadline:
                time.sleep(.01)
            client.post('/session')
            phone.send_json(self.pose(t_capture=2.0))
            with self.assertRaises(WebSocketDisconnect):
                phone.receive_json()
            self.assertEqual(client.get('/health').json()['phone'], 'down')

    def test_pose_watchdog_disarms_after_receipts_stop(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/live') as live:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(self.hello())
                phone.send_json(self.pose())
                self.next_type(live, 'pose')
                health = self.next_type(live, 'health')
                self.assertEqual(health['stop_reason'], 'pose_stale')
                self.assertEqual(health['phone'], 'stale')
                self.assertFalse(health['armed'])
