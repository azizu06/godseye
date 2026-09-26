"""Offline provider/playback checks; no paid API or physical-scene coverage."""
from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.tests.test_changes import Scene, REFS, BACKPACK, FAR


class FakeProvider:
    identity = 'fake-v1'
    def __init__(self):
        self.texts = []
    async def synthesize(self, text):
        self.texts.append(text)
        return b'\0\0' * 1600


class AudioTests(unittest.TestCase):
    def seed(self, path):
        scene = Scene(path)
        scene.remember(*REFS, ('backpack', BACKPACK))
        for _ in range(3):
            scene.see(*REFS, ('backpack', FAR), views={'backpack': 'clear'})
        event = scene.changes.events(scene.session)[-1]
        scene.db.close()
        return event

    def test_playback_and_restart_replay_synthesize_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder + '/scene.db'
            event = self.seed(path)
            provider = FakeProvider()
            for _ in range(2):
                with TestClient(create_app(path, audio_provider=provider)) as client:
                    first = client.post('/events/' + event['id'] + '/audio')
                    self.assertEqual(first.status_code, 200)
                    payload = first.json()
                    self.assertEqual(payload['status'], 'ready')
                    self.assertEqual(payload['text'], 'The backpack moved 2.0 meters.')
                    replay = client.post('/events/' + event['id'] + '/audio').json()
                    self.assertEqual(payload, replay)
                    audio = client.get(payload['audio_url'])
                    self.assertEqual(audio.headers['content-type'], 'audio/wav')
                    self.assertTrue(audio.content.startswith(b'RIFF'))
            self.assertEqual(provider.texts, ['The backpack moved 2.0 meters.'])

    def test_default_unavailable_and_unknown_event_never_call_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder + '/scene.db'
            event = self.seed(path)
            with TestClient(create_app(path)) as client:
                result = client.post('/events/' + event['id'] + '/audio').json()
                self.assertEqual(result['status'], 'unavailable')
                self.assertIsNone(result['audio_url'])
                self.assertEqual(client.post('/events/arbitrary-scene-text/audio').status_code, 404)
                self.assertEqual(client.get('/audio/' + 'a' * 64 + '.wav').status_code, 404)
                self.assertEqual(client.get('/audio/..%2Fschema.sql.wav').status_code, 404)

    def test_provider_failure_is_sanitized_and_not_retried(self):
        class Broken(FakeProvider):
            async def synthesize(self, text):
                self.texts.append(text)
                raise RuntimeError('xi-api-key: pretend-secret and private response')
        with tempfile.TemporaryDirectory() as folder:
            path = folder + '/scene.db'
            event = self.seed(path)
            provider = Broken()
            with TestClient(create_app(path, audio_provider=provider)) as client:
                for _ in range(2):
                    result = client.post('/events/' + event['id'] + '/audio')
                    self.assertEqual(result.json()['status'], 'error')
                    self.assertNotIn('pretend-secret', result.text)
                    self.assertIsNone(result.json()['audio_url'])
            self.assertEqual(len(provider.texts), 1)
            self.assertNotIn(b'pretend-secret', Path(path).read_bytes())

    def test_duration_and_map_isolation(self):
        class Long(FakeProvider):
            async def synthesize(self, text):
                return b'\0\0' * (16000 * 12 + 1)
        with tempfile.TemporaryDirectory() as folder:
            path = folder + '/scene.db'
            event = self.seed(path)
            with TestClient(create_app(path, audio_provider=Long())) as client:
                self.assertEqual(client.post('/events/' + event['id'] + '/audio').json()['status'], 'error')
            with TestClient(create_app(path, audio_provider=FakeProvider())) as client:
                # Failed identities intentionally remain failed; a provider config change gives a new identity.
                self.assertEqual(client.post('/events/' + event['id'] + '/audio').json()['status'], 'error')
            provider = FakeProvider()
            provider.identity = 'another-fake'
            with TestClient(create_app(path, audio_provider=provider)) as client:
                url = client.post('/events/' + event['id'] + '/audio').json()['audio_url']
                client.post('/session')
                self.assertEqual(client.get(url).status_code, 404)
                self.assertEqual(client.post('/events/' + event['id'] + '/audio').status_code, 404)

    def test_templates_preserve_uncertainty_and_reject_scene_prose(self):
        from backend.audio import event_text
        self.assertEqual(event_text({'kind': 'possible_move', 'displacement_m': 2}, 'backpack'),
                         'The backpack may have moved. Its identity is uncertain.')
        self.assertEqual(event_text({'kind': 'not_found'}, 'backpack'),
                         'The backpack was not found on this rescan.')
        self.assertEqual(event_text({'kind': 'new'}, 'cup'), 'A new cup was observed.')
        for event, name in [({'kind': 'moved', 'displacement_m': float('nan')}, 'cup'),
                            ({'kind': 'new'}, '<secret>'), ({'kind': 'arbitrary'}, 'cup')]:
            with self.assertRaises(ValueError):
                event_text(event, name)

    def test_elevenlabs_gate_and_fake_transport_no_credential_leak(self):
        import asyncio
        import httpx
        from backend.audio import ElevenLabsProvider
        with self.assertRaises(ValueError):
            ElevenLabsProvider('pretend-secret', 'voice')
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, content=b'\0\0' * 1600)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                provider = ElevenLabsProvider('pretend-secret', 'voice', spend_approved=True,
                                              privacy_approved=True, client=client)
                self.assertNotIn('pretend-secret', repr(provider))
                self.assertNotIn('pretend-secret', provider.identity)
                self.assertEqual(len(await provider.synthesize('A new cup was observed.')), 3200)
        asyncio.run(run())
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, 'api.elevenlabs.io')
        self.assertEqual(requests[0].url.params['output_format'], 'pcm_16000')
        self.assertEqual(requests[0].headers['xi-api-key'], 'pretend-secret')
        self.assertNotIn(b'pretend-secret', requests[0].content)

    def test_timeout_and_cache_capacity_fail_closed(self):
        import asyncio
        from unittest.mock import patch
        class Slow(FakeProvider):
            async def synthesize(self, text):
                self.texts.append(text)
                await asyncio.sleep(1)
                return b'\0\0'
        with tempfile.TemporaryDirectory() as folder:
            path = folder + '/scene.db'
            event = self.seed(path)
            provider = Slow()
            with patch('backend.audio.TIMEOUT_S', .001), TestClient(create_app(path, audio_provider=provider)) as client:
                self.assertEqual(client.post('/events/' + event['id'] + '/audio').json()['status'], 'error')
                self.assertEqual(client.post('/events/' + event['id'] + '/audio').json()['status'], 'error')
                self.assertEqual(len(provider.texts), 1)
            other = FakeProvider()
            other.identity = 'new-config'
            with patch('backend.audio.MAX_ENTRIES', 1), TestClient(create_app(path, audio_provider=other)) as client:
                self.assertEqual(client.post('/events/' + event['id'] + '/audio').status_code, 507)
                self.assertEqual(other.texts, [])

    def test_elevenlabs_rejects_redirect_and_oversized_response(self):
        import asyncio
        import httpx
        from backend.audio import ElevenLabsProvider, MAX_PCM
        async def run(status, content):
            def respond(request):
                return httpx.Response(status, content=content, headers={'Location': 'https://attacker.test'})
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                provider = ElevenLabsProvider('fake-key', 'voice', spend_approved=True,
                                              privacy_approved=True, client=client)
                with self.assertRaises(ValueError):
                    await provider.synthesize('A new cup was observed.')
        asyncio.run(run(302, b''))
        asyncio.run(run(200, b'\0' * (MAX_PCM + 2)))
