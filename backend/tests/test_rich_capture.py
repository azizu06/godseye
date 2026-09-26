"""Full sensor storage/inspection contract; synthetic data never enters the live server."""
import io
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import numpy as np
from PIL import Image

from backend.app import create_app
from backend.rich_capture import RichCapture, decode_rich, render_sensor
from backend.tests.test_map_transport import frame, hello, wait_for


def packet(kind='frame', capture=1., **changes):
    depths = np.array([[1., np.nan], [0., 8.]], dtype='<f4').tobytes()
    confidence = bytes([0, 1, 2, 255])
    header = dict(version=2, type='capture', kind=kind, session_id='map-test', map_epoch=1,
                  frame_id=1, t_capture=capture, t_wall_ms=int(time.time() * 1000),
                  metadata={'samples': [{'sensor': 'gyroscope', 'timestamp': .99, 'values': {'rotation_rad_s': [1, 2, 3]}}]},
                  sections=[dict(name='raw_depth', format='f32le', offset=0, length=16, shape=[2, 2]),
                            dict(name='raw_confidence', format='u8', offset=16, length=4, shape=[2, 2])])
    header.update(changes)
    body = depths + confidence
    if kind == 'telemetry':
        header['sections'] = []
        body = b''
    raw = json.dumps(header).encode()
    return struct.pack('<I', len(raw)) + raw + body


class RichCaptureTests(unittest.TestCase):
    def connect(self, client, phone):
        phone.send_json(hello('map-test'))
        phone.send_bytes(frame('map-test'))
        wait_for(lambda: client.get('/capture/status').json()['frame'] is not None)

    def test_lossless_storage_and_preview_only_normalizes_display(self):
        original = packet()
        value = decode_rich(original)
        self.assertEqual(value.data, original)
        array = np.frombuffer(value.section('raw_depth'), dtype='<f4')
        self.assertTrue(np.isnan(array[1]))
        self.assertEqual(array[3], 8.)
        with Image.open(io.BytesIO(render_sensor(value, 'raw_depth'))) as image:
            self.assertEqual(image.size, (2, 2))
            self.assertEqual(image.getpixel((1, 0)), (0, 0, 0))
            self.assertEqual(image.getpixel((0, 1)), (0, 0, 0))
        with Image.open(io.BytesIO(render_sensor(value, 'raw_confidence'))) as image:
            self.assertEqual(image.getpixel((0, 1)), (91, 230, 161))
            self.assertEqual(image.getpixel((1, 1)), (0, 0, 0))

    def test_rejects_inconsistent_binary_and_nonfinite_metadata(self):
        bad_sections = [
            [dict(name='raw_depth', format='f32le', offset=1, length=20, shape=[5])],
            [dict(name='raw_depth', format='f32le', offset=0, length=20, shape=[2, 2])],
            [dict(name='../escape', format='u8', offset=0, length=20, shape=[20])],
            [dict(name='color', format='unknown', offset=0, length=20, shape=[20])]]
        for sections in bad_sections:
            with self.subTest(sections=sections), self.assertRaises(ValueError):
                decode_rich(packet(sections=sections))
        for changes in [dict(metadata={'bad': float('nan')}), dict(version=2.0), dict(map_epoch=True), dict(t_capture=-1.)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                decode_rich(packet(**changes))
        with self.assertRaises(ValueError):
            decode_rich(packet()[:-1])
        with self.assertRaises(ValueError):
            decode_rich(packet(metadata={'value': 1e308}).replace(b'1e+308', b'1e+999'))

    def test_ingest_download_status_record_and_no_pose_refresh(self):
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(':memory:', capture_directory=folder)) as client:
            with client.websocket_connect('/phone') as phone:
                self.connect(client, phone)
                pose_at = client.app.state.pose_at
                raw = packet()
                result = client.post('/capture/ingest', content=raw)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(client.app.state.pose_at, pose_at)
                self.assertEqual(client.get('/capture/rich/frame.bin').content, raw)
                recorded = list(Path(folder).glob('*/*.capture'))
                self.assertEqual(len(recorded), 1)
                self.assertEqual(recorded[0].read_bytes(), raw)
                state = client.get('/capture/status').json()['rich']
                self.assertEqual(state['recorded_bytes'], len(raw))
                self.assertEqual(state['packets']['frame']['metadata']['sample_count'], 1)
                self.assertNotIn('samples', state['packets']['frame']['metadata'])
                image = client.get('/capture/sensor/raw_depth')
                self.assertEqual(image.status_code, 200)
                self.assertEqual(image.headers['content-type'], 'image/png')
                self.assertEqual(client.get('/capture/sensor/raw_depth', headers={'If-None-Match': image.headers['etag']}).status_code, 304)
                self.assertEqual(client.get('/capture/sensor/person_mask').status_code, 204)
                self.assertEqual(client.post('/capture/ingest', content=raw).status_code, 409)
                self.assertEqual(client.post('/capture/ingest', content=packet('telemetry')).status_code, 200)
                self.assertEqual(client.post('/capture/ingest', content=packet(capture=2.)).status_code, 200)
                self.assertEqual(client.app.state.rich_capture.received, 3)
            wait_for(lambda: client.get('/capture/status').json()['rich']['packets'] == {})
            self.assertEqual(client.get('/capture/rich/frame.bin').status_code, 404)
            self.assertEqual(len(list(Path(folder).glob('*/*.capture'))), 3)

    def test_rejects_wrong_session_epoch_stale_malformed_and_large_uploads(self):
        with TestClient(create_app(':memory:')) as client:
            self.assertEqual(client.post('/capture/ingest', content=packet()).status_code, 409)
            with client.websocket_connect('/phone') as phone:
                self.connect(client, phone)
                for changes in [dict(session_id='other'), dict(map_epoch=2), dict(t_wall_ms=0)]:
                    self.assertEqual(client.post('/capture/ingest', content=packet(**changes)).status_code, 409)
                for payload in [b'no', packet()[:-1], packet(metadata={'nan': float('nan')})]:
                    self.assertEqual(client.post('/capture/ingest', content=payload).status_code, 400)
                with patch('backend.capture_routes.MAX_PACKET', 5):
                    self.assertEqual(client.post('/capture/ingest', content=packet()).status_code, 413)
                self.assertEqual(client.app.state.rich_capture.received, 0)
                client.post('/capture/ingest', content=packet())
                client.post('/session')
                self.assertEqual(client.get('/capture/status').json()['rich']['packets'], {})

    def test_legacy_depth_preview_without_updated_phone(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            self.connect(client, phone)
            self.assertEqual(client.get('/capture/sensor/rgb').content, client.get('/capture/frame.jpg').content)
            self.assertEqual(client.get('/capture/sensor/raw_depth').status_code, 200)
            self.assertEqual(client.get('/capture/sensor/raw_confidence').status_code, 200)
            self.assertEqual(client.get('/capture/sensor/smoothed_depth').status_code, 204)

    def test_recording_limit_survives_reconnect_and_never_deletes_files(self):
        with tempfile.TemporaryDirectory() as folder:
            capture = RichCapture(folder)
            raw = decode_rich(packet())
            used, name, error = capture.record(raw)
            self.assertIsNone(error)
            self.assertEqual(used, len(raw.data))
            capture.reset()
            with patch('backend.rich_capture.SESSION_BUDGET', used):
                total, _, error = capture.record(decode_rich(packet(capture=2.)))
                self.assertEqual(total, used)
                self.assertIn('limit', error)
                self.assertEqual(len(list((Path(folder) / name).glob('*.capture'))), 1)
