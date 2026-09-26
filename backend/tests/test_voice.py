"""Push-to-talk Q&A with fake speech/answer providers; no paid API, microphone or real scene."""
import asyncio
import base64
import io
import json
import logging
import tempfile
import time
import unittest
import wave
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.tests.test_changes import BACKPACK, FAR, REFS, Scene
from backend.voice import MAX_CONTEXT_OBJECTS, VoiceProviders, grounding, providers_from_env

# Synthetic stand-in for a browser MediaRecorder clip: bytes are opaque to the backend.
CLIP = b'\x1aE\xdf\xa3' + bytes(range(256)) * 8
WEBM = {'Content-Type': 'audio/webm;codecs=opus'}


class FakeTranscriber:
    def __init__(self, text='Where is the backpack?', error=None):
        self.text, self.error, self.calls = text, error, []

    async def transcribe(self, audio, mime):
        self.calls.append((audio, mime))
        if self.error:
            raise self.error
        return self.text


class FakeAnswerer:
    def __init__(self, answer='The backpack was last seen near the far wall.', error=None, delay=0.):
        self.answer_text, self.error, self.delay, self.calls = answer, error, delay, []

    async def answer(self, question, context):
        self.calls.append((question, context))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.answer_text


class FakeSpeaker:
    identity = 'fake-voice'

    def __init__(self, error=None):
        self.error, self.texts = error, []

    async def synthesize(self, text, limit=None):
        self.texts.append(text)
        if self.error:
            raise self.error
        return b'\0\0' * 3200


def seeded(folder):
    """A stored map with two reference objects and a backpack that moved 2 m on rescan."""
    path = folder + '/scene.db'
    scene = Scene(path)
    scene.remember(*REFS, ('backpack', BACKPACK))
    for _ in range(3):
        scene.see(*REFS, ('backpack', FAR), views={'backpack': 'clear'})
    scene.db.close()
    return path


def providers(transcriber=None, answerer=None, speaker=None):
    return VoiceProviders(transcriber or FakeTranscriber(), answerer or FakeAnswerer(), speaker or FakeSpeaker())


class GroundingTests(unittest.TestCase):
    def objects(self, count):
        return [dict(id=f'o{i}', **{'class': 'chair'}, position=[i, 0., 1.], confidence=.9, first_seen=10.,
                     last_seen=100. - i, observations=3, state='present',
                     identity=dict(label='red chair' if i == 0 else 'guess', status='labeled' if i == 0 else 'pending',
                                   reason=None, source='gemini'))
                for i in range(count)]

    def test_context_is_bounded_and_reports_last_seen_age_not_presence(self):
        context = grounding(('s', 1), self.objects(MAX_CONTEXT_OBJECTS + 25), [], now=160., live=False)
        self.assertEqual(len(context['objects']), MAX_CONTEXT_OBJECTS)
        self.assertEqual(context['objects_total'], MAX_CONTEXT_OBJECTS + 25)
        first = context['objects'][0]
        self.assertEqual(first['last_seen_s_ago'], 60)
        self.assertEqual(first['label'], 'red chair')
        self.assertNotIn('label', context['objects'][1])  # only a completed label is a description
        self.assertNotIn('id', first)
        self.assertFalse(context['map']['live'])
        self.assertNotIn('route', context)
        self.assertNotIn('extras', context)

    def test_empty_map_and_optional_route_and_extras_are_absent_safe(self):
        context = grounding(None, [], [], now=5., live=False, route=[], extras=lambda: None)
        self.assertEqual(context['objects'], [])
        self.assertIsNone(context['map']['session_id'])
        self.assertNotIn('route', context)
        self.assertNotIn('extras', context)
        route = [[i / 10, 0., 0.] for i in range(100)]
        context = grounding(('s', 1), [], [], now=5., live=True, route=route,
                            extras=lambda: {'detections': [{'class': 'person'}]})
        self.assertLessEqual(len(context['route']['points']), 32)
        self.assertEqual(context['route']['points'][-1], [9.9, 0., 0.])
        self.assertEqual(context['extras'], {'detections': [{'class': 'person'}]})
        # Oversized or failing extras are dropped, never passed unbounded to the model.
        self.assertNotIn('extras', grounding(None, [], [], now=5., live=False, extras=lambda: {'x': 'y' * 10000}))
        self.assertNotIn('extras', grounding(None, [], [], now=5., live=False, extras=lambda: 1 / 0))


class VoiceRouteTests(unittest.TestCase):
    def test_default_backend_reports_unavailable_and_never_reads_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            with TestClient(create_app(seeded(folder))) as client:
                self.assertEqual(client.get('/voice').json(), {'version': 1, 'status': 'unavailable'})
                response = client.post('/voice/ask', content=CLIP, headers=WEBM)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()['detail'], 'Voice Q&A unavailable')

    def test_question_is_answered_from_stored_observations_and_spoken_without_retention(self):
        with tempfile.TemporaryDirectory() as folder:
            voice = providers()
            path = seeded(folder)
            with TestClient(create_app(path, voice_providers=voice)) as client:
                self.assertEqual(client.get('/voice').json(), {'version': 1, 'status': 'ready'})
                response = client.post('/voice/ask', content=CLIP, headers=WEBM)
                self.assertEqual(response.status_code, 200)
                result = response.json()
            import sqlite3
            db = sqlite3.connect(path)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            db.close()
            self.assertEqual(voice.transcriber.calls, [(CLIP, 'audio/webm')])
            question, context = voice.answerer.calls[0]
            self.assertEqual(question, 'Where is the backpack?')
            classes = {o['class'] for o in context['objects']}
            self.assertEqual(classes, {'cup', 'chair', 'backpack'})
            self.assertTrue(all(isinstance(o['last_seen_s_ago'], int) for o in context['objects']))
            self.assertEqual([c['kind'] for c in context['changes']], ['moved'])
            self.assertEqual(context['changes'][0]['class'], 'backpack')
            self.assertEqual(context['map']['session_id'], 's')
            self.assertEqual(voice.speaker.texts, ['The backpack was last seen near the far wall.'])
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['question'], 'Where is the backpack?')
            self.assertEqual(result['answer'], 'The backpack was last seen near the far wall.')
            self.assertEqual(result['evidence'], {'objects': 3, 'changes': 1})
            self.assertEqual((result['session_id'], result['map_epoch']), ('s', 1))
            self.assertEqual(result['speech']['status'], 'ready')
            audio = base64.b64decode(result['speech']['data'])
            with wave.open(io.BytesIO(audio)) as clip:
                self.assertEqual((clip.getframerate(), clip.getnchannels()), (16000, 1))
            self.assertEqual(result['speech']['mime'], 'audio/wav')
            self.assertAlmostEqual(result['speech']['duration_s'], .2)
            # Neither the recording nor the spoken reply is stored.
            self.assertNotIn('spoken_audio', tables)

    def test_empty_transcript_is_no_speech_without_answer_or_speech(self):
        with tempfile.TemporaryDirectory() as folder:
            voice = providers(FakeTranscriber('  '))
            with TestClient(create_app(seeded(folder), voice_providers=voice)) as client:
                result = client.post('/voice/ask', content=CLIP, headers=WEBM).json()
            self.assertEqual(result['status'], 'no_speech')
            self.assertIsNone(result['answer'])
            self.assertIsNone(result['speech'])
            self.assertEqual((voice.answerer.calls, voice.speaker.texts), ([], []))

    def test_invalid_uploads_are_rejected_before_any_provider_call(self):
        with tempfile.TemporaryDirectory() as folder:
            voice = providers()
            with TestClient(create_app(seeded(folder), voice_providers=voice)) as client:
                self.assertEqual(client.post('/voice/ask', content=CLIP,
                                             headers={'Content-Type': 'text/plain'}).status_code, 415)
                self.assertEqual(client.post('/voice/ask', content=b'x' * 100, headers=WEBM).status_code, 422)
                self.assertEqual(client.post('/voice/ask', content=b'x' * (3 * 1024 * 1024),
                                             headers=WEBM).status_code, 413)
            self.assertEqual(voice.transcriber.calls, [])

    def test_provider_failures_are_sanitized_and_stop_later_stages(self):
        secret = 'fake-key-should-not-leak'
        with tempfile.TemporaryDirectory() as folder:
            path = seeded(folder)
            voice = providers(FakeTranscriber(error=RuntimeError(secret)))
            with TestClient(create_app(path, voice_providers=voice)) as client:
                response = client.post('/voice/ask', content=CLIP, headers=WEBM)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()['detail'], 'Speech transcription unavailable')
            self.assertEqual(voice.answerer.calls, [])

            voice = providers(answerer=FakeAnswerer(error=RuntimeError(secret)))
            with TestClient(create_app(path, voice_providers=voice)) as client:
                response = client.post('/voice/ask', content=CLIP, headers=WEBM)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()['detail'], 'Answer unavailable')
            self.assertNotIn(secret, response.text)
            self.assertEqual(voice.speaker.texts, [])

            for bad in ['', 'x' * 2000, 'bad\x00answer', None]:
                voice = providers(answerer=FakeAnswerer(bad))
                with TestClient(create_app(path, voice_providers=voice)) as client:
                    self.assertEqual(client.post('/voice/ask', content=CLIP, headers=WEBM).status_code, 502)
                self.assertEqual(voice.speaker.texts, [])

            # Speech failure still returns the grounded text answer to read on screen.
            voice = providers(speaker=FakeSpeaker(error=RuntimeError(secret)))
            with TestClient(create_app(path, voice_providers=voice)) as client:
                response = client.post('/voice/ask', content=CLIP, headers=WEBM)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['speech'], {'status': 'error'})
            self.assertEqual(response.json()['answer'], 'The backpack was last seen near the far wall.')
            self.assertNotIn(secret, response.text)

    def test_logs_never_contain_transcripts_answers_or_audio(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = seeded(folder)
                for voice in [providers(), providers(FakeTranscriber(error=RuntimeError('boom'))),
                              providers(speaker=FakeSpeaker(error=RuntimeError('boom')))]:
                    with TestClient(create_app(path, voice_providers=voice)) as client:
                        client.post('/voice/ask', content=CLIP, headers=WEBM)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        text = '\n'.join(str(record.getMessage()) for record in records)
        self.assertNotIn('backpack', text.lower())
        self.assertNotIn('boom', text)

    def test_one_question_in_flight_and_process_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            voice = providers(answerer=FakeAnswerer(delay=.5))
            with TestClient(create_app(seeded(folder), voice_providers=voice, voice_budget=2)) as client:
                import threading
                results = []
                first = threading.Thread(target=lambda: results.append(
                    client.post('/voice/ask', content=CLIP, headers=WEBM).status_code))
                first.start()
                deadline = time.monotonic() + 3
                while not voice.answerer.calls and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(client.post('/voice/ask', content=CLIP, headers=WEBM).status_code, 429)
                first.join()
                self.assertEqual(results, [200])
                self.assertEqual(client.post('/voice/ask', content=CLIP, headers=WEBM).status_code, 200)
                limited = client.post('/voice/ask', content=CLIP, headers=WEBM)
                self.assertEqual(limited.status_code, 429)
                self.assertIn('limit reached', limited.json()['detail'])
                client.post('/session')  # a new map does not reset spend
                self.assertEqual(client.post('/voice/ask', content=CLIP, headers=WEBM).status_code, 429)
            self.assertEqual(len(voice.transcriber.calls), 2)


class AdapterTests(unittest.TestCase):
    def mocked(self, module, respond):
        client_type = httpx.AsyncClient
        return patch(f'{module}.httpx.AsyncClient', side_effect=lambda **kwargs: client_type(
            transport=httpx.MockTransport(respond), **kwargs))

    def test_elevenlabs_transcription_uses_scribe_multipart_and_sanitizes(self):
        from backend.audio import ElevenLabsProvider
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'text': ' Where is the chair? ', 'language_code': 'en', 'words': []})
        def provider(respond):
            return ElevenLabsProvider('fake-key', 'voice1', spend_approved=True, privacy_approved=True,
                                      client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        text = asyncio.run(provider(respond).transcribe(CLIP, 'audio/webm'))
        self.assertEqual(text, 'Where is the chair?')
        request = requests[0]
        self.assertEqual(str(request.url), 'https://api.elevenlabs.io/v1/speech-to-text')
        self.assertEqual(request.headers['xi-api-key'], 'fake-key')
        self.assertNotIn('fake-key', str(request.url))
        body = request.content
        self.assertIn(b'name="model_id"\r\n\r\nscribe_v2', body)
        self.assertIn(b'Content-Type: audio/webm', body)
        self.assertIn(CLIP, body)
        for response in [httpx.Response(401, text='fake-key'), httpx.Response(200, json={'nope': 1})]:
            with self.assertRaisesRegex(ValueError, '^Speech provider failed$'):
                asyncio.run(provider(lambda request, r=response: r).transcribe(CLIP, 'audio/webm'))

    def test_gemini_answer_sends_only_question_and_grounding_text(self):
        from backend.labels import GeminiLabels
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {
                'parts': [{'text': '{"answer":"The chair was last seen 5 seconds ago."}'}]}}]})
        context = grounding(('s', 1), [], [], now=5., live=True)
        with self.mocked('backend.labels', respond):
            answer = asyncio.run(GeminiLabels('fake-key', 'fake-model').answer('Where is the chair?', context))
        self.assertEqual(answer, 'The chair was last seen 5 seconds ago.')
        body = json.loads(requests[0].content)
        self.assertIn('never', body['systemInstruction']['parts'][0]['text'].lower())
        text = body['contents'][0]['parts'][0]['text']
        self.assertIn('Where is the chair?', text)
        self.assertIn(json.dumps(context, separators=(',', ':')), text)
        self.assertNotIn('inlineData', json.dumps(body))
        self.assertEqual(requests[0].headers['x-goog-api-key'], 'fake-key')
        with self.mocked('backend.labels', lambda request: httpx.Response(500, text='fake-key')):
            with self.assertRaisesRegex(RuntimeError, '^Gemini answer unavailable$'):
                asyncio.run(GeminiLabels('fake-key', 'fake-model').answer('q', context))

    def test_environment_requires_explicit_enable_and_complete_configuration(self):
        complete = {'GODSEYE_VOICE_ENABLED': '1', 'ELEVENLABS_API_KEY': 'k', 'GODSEYE_ELEVENLABS_VOICE_ID': 'v1',
                    'GEMINI_API_KEY': 'g', 'GODSEYE_GEMINI_MODEL': 'm'}
        with patch.dict('os.environ', {k: v for k, v in complete.items() if k != 'GODSEYE_VOICE_ENABLED'}, clear=True):
            self.assertIsNone(providers_from_env())
        for missing in ['ELEVENLABS_API_KEY', 'GODSEYE_ELEVENLABS_VOICE_ID', 'GEMINI_API_KEY', 'GODSEYE_GEMINI_MODEL']:
            with patch.dict('os.environ', {k: v for k, v in complete.items() if k != missing}, clear=True):
                with self.assertRaises(ValueError):
                    providers_from_env()
        with patch.dict('os.environ', complete, clear=True):
            voice = providers_from_env()
        self.assertIs(voice.transcriber, voice.speaker)


if __name__ == '__main__':
    unittest.main()
