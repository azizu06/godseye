"""On-demand speech for stored change evidence. No provider is enabled by default."""
import asyncio
import hashlib
import io
import json
import math
import re
import wave

from fastapi import HTTPException
from fastapi.responses import Response

RATE = 16000
MAX_SECONDS = 12
MAX_PCM = RATE * 2 * MAX_SECONDS
MAX_ENTRIES = 256  # reject new identities at capacity; never evict and bill a replay again
TIMEOUT_S = 15


def event_text(event, class_name):
    """Only templates and bounded detector class names; never accept scene prose."""
    if not isinstance(class_name, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z _-]{0,39}', class_name):
        raise ValueError('Invalid class')
    name = class_name.lower().replace('_', ' ')
    kind = event.get('kind')
    if kind == 'new':
        return f'A new {name} was observed.'
    if kind == 'not_found':
        return f'The {name} was not found on this rescan.'
    if kind in ('moved', 'possible_move'):
        distance = event.get('displacement_m')
        if type(distance) not in (int, float) or not math.isfinite(distance) or not 0 <= distance <= 100:
            raise ValueError('Invalid displacement')
        if kind == 'possible_move':
            return f'The {name} may have moved. Its identity is uncertain.'
        return f'The {name} moved {distance:.1f} meters.'
    raise ValueError('Invalid event kind')


def wav_audio(pcm, limit=MAX_PCM):
    if not isinstance(pcm, bytes) or not pcm or len(pcm) > limit or len(pcm) % 2:
        raise ValueError('Invalid PCM audio')
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(pcm)
    return output.getvalue()


class ElevenLabsProvider:
    """Explicit injection only, after separate credential/spend/privacy approval.

    Credentials are held only in memory; repr, errors, and cache identity exclude them.
    No automatic retries. `client` is the httpx-compatible test transport seam.
    """
    def __init__(self, api_key, voice_id, *, spend_approved=False, privacy_approved=False,
                 model_id='eleven_flash_v2_5', stt_model_id='scribe_v2', client=None):
        if not spend_approved or not privacy_approved:
            raise ValueError('ElevenLabs requires spend and privacy approval')
        if not api_key or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', voice_id):
            raise ValueError('ElevenLabs configuration unavailable')
        if not all(re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', m) for m in (model_id, stt_model_id)):
            raise ValueError('Invalid model')
        self._key, self._voice, self._model, self._client = api_key, voice_id, model_id, client
        self._stt_model = stt_model_id
        self.identity = f'elevenlabs:{voice_id}:{model_id}:pcm16000:v1'

    async def synthesize(self, text, limit=MAX_PCM):
        async def convert(client):
            # Stream and cap the response before allocation; redirects cannot carry the key elsewhere.
            async with client.stream('POST', f'https://api.elevenlabs.io/v1/text-to-speech/{self._voice}',
                    params={'output_format': 'pcm_16000'}, headers={'xi-api-key': self._key},
                    json={'text': text, 'model_id': self._model}, timeout=TIMEOUT_S,
                    follow_redirects=False) as response:
                if response.status_code != 200:
                    raise ValueError('Audio provider failed')
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > limit:
                        raise ValueError('Audio exceeds limit')
                    data.extend(chunk)
                return bytes(data)
        return await self._with_client(convert)

    async def transcribe(self, audio, mime):
        """Scribe speech-to-text of one push-to-talk clip; returns text or raises a sanitized error."""
        async def convert(client):
            try:
                response = await client.post('https://api.elevenlabs.io/v1/speech-to-text',
                    headers={'xi-api-key': self._key}, data={'model_id': self._stt_model},
                    files={'file': ('question', audio, mime)}, timeout=TIMEOUT_S, follow_redirects=False)
                if response.status_code != 200 or len(response.content) > 262144:
                    raise ValueError
                text = response.json()['text']
                if not isinstance(text, str):
                    raise ValueError
                return text.strip()
            except Exception:
                raise ValueError('Speech provider failed') from None
        return await self._with_client(convert)

    async def _with_client(self, call):
        import httpx
        if self._client is not None:
            return await call(self._client)
        async with httpx.AsyncClient(trust_env=False) as client:
            return await call(client)


def register_audio_routes(app, provider):
    # One call per app at a time; busy requests are rejected, never queued without bounds.
    busy = asyncio.Lock()

    def session():
        return app.state.session or app.state.objects.latest_session()

    def table():
        app.state.db.execute('CREATE TABLE IF NOT EXISTS spoken_audio ('
            'id TEXT PRIMARY KEY, session_id TEXT NOT NULL, map_epoch INTEGER NOT NULL, '
            'event_id TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, '
            'audio BLOB, duration_s REAL)')

    def metadata(row):
        key, sid, epoch, event_id, text, status, _, duration = row
        return dict(version=1, session_id=sid, map_epoch=epoch, event_id=event_id,
                    text=text, status=status, duration_s=duration,
                    audio_url=f'/audio/{key}.wav' if status == 'ready' else None)

    @app.post('/events/{event_id}/audio')
    async def synthesize(event_id: str):
        current = session()
        event = next((e for e in app.state.changes.events(current) if e['id'] == event_id), None)
        if event is None:
            raise HTTPException(404, 'No stored event in the current map')
        row = app.state.db.execute('SELECT class FROM objects WHERE id=? AND session_id=? AND map_epoch=?',
                                   (event['object_id'], *current)).fetchone()
        try:
            text = event_text(event, row[0] if row else None)
        except ValueError:
            raise HTTPException(422, 'Change evidence cannot be narrated') from None
        if provider is None:
            return dict(version=1, session_id=current[0], map_epoch=current[1], event_id=event_id,
                        text=text, status='unavailable', audio_url=None, duration_s=None)
        key = hashlib.sha256(json.dumps([current, event_id, text, provider.identity],
                                       separators=(',', ':')).encode()).hexdigest()
        table()
        cached = app.state.db.execute('SELECT * FROM spoken_audio WHERE id=?', (key,)).fetchone()
        if cached:
            return metadata(cached)
        if busy.locked():
            raise HTTPException(429, 'Audio synthesis busy; retry later')
        async with busy:
            if app.state.db.execute('SELECT COUNT(*) FROM spoken_audio').fetchone()[0] >= MAX_ENTRIES:
                raise HTTPException(507, 'Audio cache full; new synthesis unavailable')
            # Persist a failure tombstone BEFORE calling: crash/restart must not repeat a paid call.
            with app.state.db:
                app.state.db.execute('INSERT INTO spoken_audio VALUES(?,?,?,?,?,?,NULL,NULL)',
                                     (key, *current, event_id, text, 'error'))
            try:
                pcm = await asyncio.wait_for(provider.synthesize(text), timeout=TIMEOUT_S)
                audio = wav_audio(pcm)
            except Exception:
                # Never log provider exceptions, response bodies, or request headers.
                audio = None
            if audio is not None:
                with app.state.db:
                    app.state.db.execute('UPDATE spoken_audio SET status=?,audio=?,duration_s=? WHERE id=?',
                                         ('ready', audio, len(pcm) / (RATE * 2), key))
            if session() != current:
                raise HTTPException(409, 'Map changed during synthesis')
            return metadata(app.state.db.execute('SELECT * FROM spoken_audio WHERE id=?', (key,)).fetchone())

    @app.get('/audio/{audio_id}.wav')
    async def playback(audio_id: str):
        if not re.fullmatch(r'[a-f0-9]{64}', audio_id):
            raise HTTPException(404, 'Audio unavailable')
        table()
        row = app.state.db.execute('SELECT session_id,map_epoch,audio FROM spoken_audio '
                                   'WHERE id=? AND status=?', (audio_id, 'ready')).fetchone()
        if row is None or (row[0], row[1]) != session():
            raise HTTPException(404, 'Audio unavailable in this map')
        return Response(row[2], media_type='audio/wav', headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
