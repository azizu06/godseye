"""Synthetic image/provider acceptance at /phone, /objects and /ask."""
import io
import unittest
from PIL import Image
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.tests.test_objects import FakeDetector, LEFT_CUP
from backend.tests.test_map_transport import frame, hello, wait_for


class FakeLabels:
    def __init__(self):
        self.crops = []

    async def identify(self, jpeg, class_name):
        self.crops.append((Image.open(io.BytesIO(jpeg)).size, class_name))
        return 'red ceramic coffee mug'


class LabelTests(unittest.TestCase):
    def test_new_object_crop_is_labeled_once_and_search_uses_saved_facts(self):
        provider = FakeLabels()
        with TestClient(create_app(':memory:', detector=FakeDetector(LEFT_CUP),
                                   label_provider=provider)) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame())
                wait_for(lambda: provider.crops)
                wait_for(lambda: client.get('/objects').json()['objects'][0]['identity']['status'] == 'labeled')
                phone.send_bytes(frame(frame_id=2, t_capture=2.))
                wait_for(lambda: client.get('/objects').json()['objects'][0]['observations'] == 2)
                answer = client.post('/ask', json={'question': "where's my red mug?"}).json()
                self.assertEqual(answer['status'], 'ok')
                self.assertEqual(answer['matches'][0]['identity']['label'], 'red ceramic coffee mug')
                self.assertEqual(answer['matches'][0]['position'], [-1., 2., 3.2])
                self.assertEqual(provider.crops, [((8, 12), 'cup')])
                missing = client.post('/ask', json={'question': 'where is my backpack?'}).json()
                self.assertEqual(missing['status'], 'no_match')
                self.assertEqual(missing['matches'], [])

    def test_restart_search_is_scoped_and_does_not_call_provider(self):
        import tempfile
        from pathlib import Path
        provider = FakeLabels()
        with tempfile.TemporaryDirectory(dir='.') as directory:
            path = str(Path(directory) / 'objects.db')
            with TestClient(create_app(path, detector=FakeDetector(LEFT_CUP), label_provider=provider,
                                       capture_directory='')) as client:
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(hello())
                    phone.send_bytes(frame())
                    wait_for(lambda: client.get('/objects').json()['objects'] and
                             client.get('/objects').json()['objects'][0]['identity']['status'] == 'labeled')
            with TestClient(create_app(path, label_provider=provider, capture_directory='')) as client:
                answer = client.post('/ask', json={'question': 'red mug'}).json()
                self.assertEqual(answer['status'], 'ok')
                self.assertEqual(answer['matches'][0]['observations'], 1)
                client.post('/session')
                self.assertEqual(client.post('/ask', json={'question': 'red mug'}).json()['status'], 'empty')
        self.assertEqual(len(provider.crops), 1)

    def test_unavailable_unknown_error_and_timeout_preserve_class_search(self):
        import asyncio
        class Reply:
            def __init__(self, result):
                self.result = result
                self.calls = 0

            async def identify(self, jpeg, class_name):
                self.calls += 1
                if self.result == 'hang':
                    await asyncio.Event().wait()
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

        cases = [(None, 'unavailable', 'disabled'), (Reply(None), 'unknown', 'inconclusive'),
                 (Reply(RuntimeError('secret must not leak')), 'error', 'provider_error'),
                 (Reply('hang'), 'error', 'timeout'), (Reply('x' * 121), 'error', 'provider_error')]
        for provider, status, reason in cases:
            with self.subTest(status=status, reason=reason):
                with TestClient(create_app(':memory:', detector=FakeDetector(LEFT_CUP),
                                           label_provider=provider, label_timeout_s=.02)) as client:
                    with client.websocket_connect('/phone') as phone:
                        phone.send_json(hello())
                        phone.send_bytes(frame())
                        def settled():
                            objects = client.get('/objects').json()['objects']
                            return objects and objects[0]['identity']['status'] == status
                        wait_for(settled)
                        answer = client.post('/ask', json={'question': 'cup'}).json()
                        identity = answer['matches'][0]['identity']
                        self.assertEqual(identity['reason'], reason)
                        self.assertIsNone(identity['label'])
                        self.assertNotIn('secret', str(answer))
                        phone.send_bytes(frame(frame_id=2, t_capture=2.))
                        wait_for(lambda: client.get('/objects').json()['objects'][0]['observations'] == 2)
                if provider:
                    self.assertEqual(provider.calls, 1)

    def test_minimal_crop_is_clipped_and_capped(self):
        from backend.labels import crop_jpeg
        image = Image.new('RGB', (1000, 800))
        self.assertEqual(Image.open(io.BytesIO(crop_jpeg(image, (-20, 0, 400, 800)))).size, (128, 256))
        self.assertEqual(crop_jpeg(image, (1001, 0, 1100, 10)), b'')

    def test_queue_and_attempt_budget_are_bounded_even_after_merge_and_restart(self):
        from backend.labels import ObjectLabels
        from backend.objects import ObjectMemory
        from backend.tests.test_objects import open_db, located, RIGHT_CUP
        db = open_db(frames=5)
        memory = ObjectMemory(db)
        labels = ObjectLabels(db, FakeLabels(), lambda session: None, queue_size=1, max_attempts=2)
        first = memory.record(('s', 1), 1, 1., [located(LEFT_CUP, (0, 0, 0)), located(RIGHT_CUP, (2, 0, 0))])
        labels.enqueue(('s', 1), first, (b'crop', b'crop'))
        saved = memory.snapshot(('s', 1))
        self.assertEqual({obj['identity']['status'] for obj in saved}, {'pending', 'unavailable'})
        self.assertEqual(next(obj for obj in saved if obj['identity']['status'] == 'unavailable')['identity']['reason'], 'queue_full')
        # A fresh worker retains the spent attempt; interrupted calls never retry.
        labels = ObjectLabels(db, FakeLabels(), lambda session: None, max_attempts=2)
        second = memory.record(('s', 1), 2, 2., [located(LEFT_CUP, (4, 0, 0))])
        labels.enqueue(('s', 1), second, (b'crop',))
        with db:
            memory.merge(first[0].object_id, second[0].object_id, (0, 0, 0), 'present')
        third = memory.record(('s', 1), 3, 3., [located(LEFT_CUP, (6, 0, 0))])
        labels.enqueue(('s', 1), third, (b'crop',))
        self.assertEqual(next(obj for obj in memory.snapshot(('s', 1)) if obj['id'] == third[0].object_id)['identity']['reason'], 'limit')
        db.close()

    def test_http_adapter_handles_fake_gemini_response_and_sanitizes_failures(self):
        import asyncio
        import base64
        import json
        from unittest.mock import patch
        import httpx
        from backend.labels import GeminiLabels
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {
                'parts': [{'text': '{"label":"red mug"}'}]}}]})
        client_type = httpx.AsyncClient
        with patch('backend.labels.httpx.AsyncClient',
                   side_effect=lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs)):
            label = asyncio.run(GeminiLabels('fake-key', 'fake-model').identify(b'crop-only', 'cup'))
        self.assertEqual(label, 'red mug')
        body = json.loads(requests[0].content)
        parts = body['contents'][0]['parts']
        self.assertEqual(base64.b64decode(parts[1]['inlineData']['data']), b'crop-only')
        self.assertNotIn('fake-key', str(requests[0].url))
        self.assertEqual(requests[0].headers['x-goog-api-key'], 'fake-key')
        for response in [httpx.Response(429, text='fake-key'), httpx.Response(200, json={'candidates': []})]:
            with patch('backend.labels.httpx.AsyncClient', side_effect=lambda **kwargs: client_type(
                    transport=httpx.MockTransport(lambda request: response), **kwargs)):
                with self.assertRaisesRegex(RuntimeError, '^Gemini identification unavailable$'):
                    asyncio.run(GeminiLabels('fake-key', 'fake-model').identify(b'crop', 'cup'))

    def test_search_finds_old_object_outside_live_snapshot(self):
        from backend.localization import Detection
        detector = FakeDetector(Detection(LEFT_CUP.box, 'backpack', .8))
        with TestClient(create_app(':memory:', detector=detector)) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(hello())
                phone.send_bytes(frame())
                wait_for(lambda: len(client.get('/objects').json()['objects']) == 1)
                detector.detections = [LEFT_CUP] * 256
                phone.send_bytes(frame(frame_id=2, t_capture=2.))
                wait_for(lambda: len(client.get('/objects').json()['objects']) == 256)
                self.assertTrue(all(obj['class'] == 'cup' for obj in client.get('/objects').json()['objects']))
                answer = client.post('/ask', json={'question': 'where is my backpack?'}).json()
                self.assertEqual([obj['class'] for obj in answer['matches']], ['backpack'])

    def test_key_alone_does_not_enable_uploads_and_opt_in_requires_configuration(self):
        from unittest.mock import patch
        from backend.labels import provider_from_env
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'fake-key'}, clear=True):
            self.assertIsNone(provider_from_env())
        with patch.dict('os.environ', {'GODSEYE_GEMINI_ENABLED': '1'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'requires a key'):
                provider_from_env()
