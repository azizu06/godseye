"""Real /phone -> browser preview routes, without a phone or model weights."""
import io
import json
import struct
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app, decode_frame
from backend.capture import CaptureBuffer
from backend.tests.test_map_transport import frame, hello, wait_for


class CaptureTests(unittest.TestCase):
    def wait_frame(self, client, frame_id):
        def arrived():
            latest = client.get('/capture/status').json()['frame']
            return latest is not None and latest['metadata']['frame_id'] == frame_id
        wait_for(arrived)

    def test_page_and_empty_state(self):
        with TestClient(create_app(':memory:')) as client:
            page = client.get('/capture')
            self.assertEqual(page.status_code, 200)
            self.assertIn('text/html', page.headers['content-type'])
            self.assertIn('Live capture', page.text)
            self.assertEqual(page.headers['cache-control'], 'no-store')
            status = client.get('/capture/status')
            self.assertIsNone(status.json()['frame'])
            self.assertEqual(status.json()['health']['phone'], 'down')
            self.assertFalse(status.json()['health']['armed'])
            self.assertEqual(status.headers['cache-control'], 'no-store')
            self.assertEqual(client.get('/capture/frame.jpg').status_code, 204)
            self.assertEqual(client.get('/capture/frame.bin').status_code, 404)

    def test_real_bundle_jpeg_metadata_and_conditional_fetch(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(hello())
            payload = frame(frame_id=17)
            phone.send_bytes(payload)
            self.wait_frame(client, 17)
            response = client.get('/capture/frame.jpg')
            metadata = decode_frame(payload)
            start = 4 + struct.unpack_from('<I', payload)[0]
            self.assertEqual(response.content, payload[start:start + metadata.image.jpeg_len])
            self.assertEqual(response.headers['content-type'], 'image/jpeg')
            self.assertEqual(response.headers['x-frame-id'], '17')
            self.assertEqual(response.headers['cache-control'], 'no-store')
            with Image.open(io.BytesIO(response.content)) as decoded:
                self.assertEqual(decoded.size, (metadata.image.width, metadata.image.height))
            unchanged = client.get('/capture/frame.jpg', headers={'If-None-Match': response.headers['etag']})
            self.assertEqual(unchanged.status_code, 304)
            self.assertEqual(unchanged.content, b'')
            self.assertEqual(client.get('/capture/frame.bin').content, payload)
            state = client.get('/capture/status').json()
            self.assertEqual(state['frame']['metadata'], metadata.model_dump())
            self.assertEqual(state['received_frames'], 1)
            self.assertGreaterEqual(state['frame']['age_ms'], 0)
            phone.send_bytes(frame(frame_id=18, t_capture=2.))
            self.wait_frame(client, 18)
            newer = client.get('/capture/frame.jpg', headers={'If-None-Match': response.headers['etag']})
            self.assertEqual(newer.status_code, 200)
            self.assertNotEqual(newer.headers['etag'], response.headers['etag'])
            self.assertEqual(client.get('/capture/status').json()['received_frames'], 2)

    def test_old_or_stale_frames_do_not_replace_preview(self):
        for invalid in [dict(t_capture=.5), dict(t_wall_ms=0, t_capture=2.)]:
            with self.subTest(invalid=invalid), TestClient(create_app(':memory:')) as client:
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())
                    phone.send_bytes(frame(frame_id=1))
                    self.wait_frame(client, 1)
                    token = client.get('/capture/status').json()['frame']['capture_id']
                    payload = frame(frame_id=2)
                    size = struct.unpack_from('<I', payload)[0]
                    metadata = json.loads(payload[4:4 + size])
                    metadata.update(invalid)
                    header = json.dumps(metadata).encode()
                    phone.send_bytes(struct.pack('<I', len(header)) + header + payload[4 + size:])
                    wait_for(lambda: client.get('/health').json()['stop_reason'] == 'pose_stale')
                    state = client.get('/capture/status').json()
                    self.assertEqual(state['frame']['capture_id'], token)
                    self.assertEqual(state['received_frames'], 1)

    def test_disconnect_and_session_reset_clear_frame(self):
        with TestClient(create_app(':memory:')) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame())
                self.wait_frame(client, 1)
            wait_for(lambda: client.get('/capture/status').json()['frame'] is None)
            self.assertEqual(client.get('/capture/frame.jpg').status_code, 204)
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello('new-session'))
                phone.send_bytes(frame('new-session'))
                self.wait_frame(client, 1)
                client.post('/session')
                self.assertIsNone(client.get('/capture/status').json()['frame'])
                self.assertEqual(client.get('/capture/frame.bin').status_code, 404)
                phone.send_bytes(frame('new-session', t_capture=2.))
                with self.assertRaises(WebSocketDisconnect):
                    phone.receive_json()
                self.assertIsNone(client.get('/capture/status').json()['frame'])

    def test_rejected_second_phone_does_not_clear_owner_preview(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as first:
            first.send_json(hello())
            first.send_bytes(frame())
            self.wait_frame(client, 1)
            with client.websocket_connect('/phone') as second:
                second.send_json(hello('other'))
                with self.assertRaises(WebSocketDisconnect):
                    second.receive_json()
            self.assertEqual(client.get('/capture/frame.jpg').status_code, 200)
            self.assertEqual(client.get('/capture/status').json()['received_frames'], 1)

    def test_buffer_is_bounded_and_frame_age_survives_stale_stream(self):
        buffer = CaptureBuffer()
        with patch('backend.capture.time.monotonic', return_value=10) as clock:
            for index in range(200):
                clock.return_value = 10 + index / 10
                buffer.update(str(index).encode(), {'frame_id': index})
            self.assertEqual(buffer.latest.payload, b'199')
            self.assertEqual(len(buffer.receipts), 120)
            self.assertAlmostEqual(buffer.status()['frame_hz'], 10)
            clock.return_value += 3
            self.assertEqual(buffer.status()['frame_hz'], 0)
            self.assertAlmostEqual(buffer.status()['frame']['age_ms'], 3000)
            buffer.clear()
            self.assertIsNone(buffer.status()['frame'])
            self.assertEqual(buffer.status()['received_frames'], 0)
