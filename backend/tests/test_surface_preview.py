"""Compact preview preserves source samples and enforces live map freshness."""
import json
import struct
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.capture_routes import legacy_packet
from backend.capture import CaptureBuffer
from backend.rich_capture import decode_rich
from backend.surface_preview import surface_payload
from backend.tests.test_map_transport import frame, hello, pose, wait_for


def rich_frame(payload):
    n = int.from_bytes(payload[:4], 'little')
    header = json.loads(payload[4:4 + n])
    buffer = CaptureBuffer()
    buffer.update(payload, header)
    legacy = legacy_packet(buffer.latest)
    sections = legacy.header['sections']
    body = payload[4 + n:]
    # Stand in for lossless color and features that must remain in the archive.
    extra = bytes(256_000)
    header = dict(version=2, type='capture', kind='frame',
                  **{k: header[k] for k in ('session_id', 'map_epoch', 'frame_id', 't_capture', 't_wall_ms')},
                  metadata=dict(tracking=header['tracking'], transform=header['transform'],
                                native_image=header['image']),
                  sections=[*sections, dict(name='color_y', format='u8', offset=len(body),
                                            length=len(extra), shape=[len(extra)])])
    encoded = json.dumps(header).encode()
    return struct.pack('<I', len(encoded)) + encoded + body + extra


class SurfacePreviewTests(unittest.TestCase):
    def connect(self, client, phone):
        phone.send_json(hello())
        payload = frame()
        phone.send_bytes(payload)
        wait_for(lambda: client.app.state.capture.latest is not None)
        return payload

    def test_compact_packet_preserves_calibration_and_exact_image_depth_confidence(self):
        full = decode_rich(rich_frame(frame()))
        compact = decode_rich(surface_payload(full))
        self.assertLess(len(compact.data), len(full.data) / 10)
        self.assertEqual(compact.header['metadata'], full.header['metadata'])
        for key in ('session_id', 'map_epoch', 'frame_id', 't_capture', 't_wall_ms'):
            self.assertEqual(compact.header[key], full.header[key])
        for name in ('rgb', 'raw_depth', 'raw_confidence'):
            self.assertEqual(compact.section(name), full.section(name))
        self.assertIsNone(compact.section('color_y'))
        self.assertEqual(len(full.section('color_y')), 256_000)

    def test_conditional_cross_origin_preview_and_original_download_are_independent(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            original = self.connect(client, phone)
            legacy = client.get('/capture/surface.bin')
            self.assertEqual(legacy.content, original)
            full = rich_frame(original)
            self.assertEqual(client.post('/capture/ingest', content=full).status_code, 200)
            origin = {'Origin': 'http://localhost:5173'}
            response = client.get('/capture/surface.bin', headers=origin)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, surface_payload(decode_rich(full)))
            self.assertNotEqual(response.headers['etag'], legacy.headers['etag'])
            self.assertIn('ETag', response.headers['access-control-expose-headers'])
            self.assertGreaterEqual(float(response.headers['x-capture-age-ms']), 0)
            unchanged = client.get('/capture/surface.bin', headers=dict(origin, **{'If-None-Match': response.headers['etag']}))
            self.assertEqual((unchanged.status_code, unchanged.content), (304, b''))
            self.assertEqual(client.get('/capture/rich/frame.bin').content, full)
            preflight = client.options('/capture/surface.bin', headers=dict(origin, **{
                'Access-Control-Request-Method': 'GET', 'Access-Control-Request-Headers': 'If-None-Match'}))
            self.assertEqual(preflight.status_code, 200)
            self.assertIn('If-None-Match', preflight.headers['access-control-allow-headers'])

    def test_prefer_fresh_legacy_over_lagging_rich_and_never_reuse_pre_tracking_loss_frame(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            original = self.connect(client, phone)
            self.assertEqual(client.post('/capture/ingest', content=rich_frame(original)).status_code, 200)
            newer = frame(t_capture=1.4, frame_id=2)
            phone.send_bytes(newer)
            wait_for(lambda: client.app.state.capture.latest.metadata['frame_id'] == 2)
            response = client.get('/capture/surface.bin')
            self.assertEqual(response.content, newer)
            phone.send_json(pose(1.5, 'limited'))
            wait_for(lambda: client.app.state.tracking_lost_capture == 1.5)
            self.assertEqual(client.get('/capture/surface.bin').status_code, 204)
            phone.send_json(pose(1.6))
            wait_for(lambda: client.app.state.pose.t_capture == 1.6)
            # A cached token cannot make an old frame eligible again.
            self.assertEqual(client.get('/capture/surface.bin', headers={
                'If-None-Match': response.headers['etag']}).status_code, 204)
            recovered = frame(t_capture=1.7, frame_id=3)
            phone.send_bytes(recovered)
            wait_for(lambda: client.app.state.capture.latest.metadata['frame_id'] == 3)
            self.assertEqual(client.get('/capture/surface.bin').content, recovered)

    def test_stale_disconnected_and_replaced_sessions_do_not_serve_preview(self):
        with TestClient(create_app(':memory:')) as client:
            self.assertEqual(client.get('/capture/surface.bin').status_code, 204)
            with client.websocket_connect('/phone') as phone:
                self.connect(client, phone)
                with patch('backend.capture_routes.time.monotonic', return_value=time.monotonic() + 2):
                    self.assertEqual(client.get('/capture/surface.bin').status_code, 204)
                client.post('/session')
                self.assertEqual(client.get('/capture/surface.bin').status_code, 204)
            self.assertEqual(client.get('/capture/surface.bin').status_code, 204)

    def test_tracking_loss_during_off_thread_pack_discards_the_response(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            original = self.connect(client, phone)
            client.post('/capture/ingest', content=rich_frame(original))

            def lose_tracking(packet):
                client.app.state.tracking_lost_capture = packet.header['t_capture']
                return surface_payload(packet)

            with patch('backend.capture_routes.surface_payload', side_effect=lose_tracking):
                self.assertEqual(client.get('/capture/surface.bin').status_code, 204)
