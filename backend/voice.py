"""Push-to-talk Q&A: speech -> text -> grounded answer -> speech. Off unless providers are injected.

Nothing is retained: the uploaded clip, transcript, answer and reply audio live only for one
request. Nothing here logs question, answer or audio content, and provider errors are sanitized.
"""
import asyncio
import base64
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException, Request

from backend.audio import RATE, ElevenLabsProvider, wav_audio

MIN_AUDIO = 1024
MAX_AUDIO = 2 * 1024 * 1024  # ~60 s of browser Opus; a push-to-talk question is far shorter
AUDIO_TYPES = {'audio/webm', 'audio/ogg', 'audio/mp4', 'audio/mpeg', 'audio/wav', 'audio/x-wav', 'audio/aac'}
MAX_QUESTION = 500
MAX_ANSWER = 600
MAX_REPLY_PCM = RATE * 2 * 30
MAX_CONTEXT_OBJECTS = 40
MAX_CONTEXT_CHANGES = 20
MAX_ROUTE_POINTS = 32
MAX_EXTRAS = 4096
MAX_FRAME_DETECTIONS = 16
MAX_APPROACH_POINTS = 16
TRANSCRIBE_S, ANSWER_S, SPEAK_S = 15, 12, 15
DEFAULT_BUDGET = 100  # paid questions per backend process (a new map does not reset it); never retried


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, mime: str) -> str: ...


class Answerer(Protocol):
    async def answer(self, question: str, context: dict) -> str: ...


class Speaker(Protocol):
    async def synthesize(self, text: str, limit: int) -> bytes: ...


@dataclass(frozen=True)
class VoiceProviders:
    transcriber: Transcriber
    answerer: Answerer
    speaker: Speaker


def providers_from_env():
    """A key by itself never enables uploads or spend; enabled but incomplete fails startup."""
    if os.environ.get('GODSEYE_VOICE_ENABLED') != '1':
        return None
    from backend.labels import GeminiLabels
    env = os.environ.get
    if not env('ELEVENLABS_API_KEY') or not env('GODSEYE_ELEVENLABS_VOICE_ID'):
        raise ValueError('Voice Q&A requires ElevenLabs key and voice')
    # Setting GODSEYE_VOICE_ENABLED=1 is the operator's explicit spend and privacy approval.
    eleven = ElevenLabsProvider(env('ELEVENLABS_API_KEY'), env('GODSEYE_ELEVENLABS_VOICE_ID'),
                                spend_approved=True, privacy_approved=True,
                                model_id=env('GODSEYE_ELEVENLABS_TTS_MODEL', 'eleven_flash_v2_5'),
                                stt_model_id=env('GODSEYE_ELEVENLABS_STT_MODEL', 'scribe_v2'))
    gemini = GeminiLabels(env('GEMINI_API_KEY', ''), env('GODSEYE_GEMINI_MODEL', ''))
    return VoiceProviders(eleven, gemini, eleven)


def _age(now, t):
    return None if not isinstance(t, (int, float)) else max(0, round(now - t))


def _sample(points, limit):
    """At most `limit` points spread along the path, always keeping both ends."""
    n = len(points)
    keep = sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)}) if n > limit else range(n)
    return [[round(float(v), 2) for v in points[i]] for i in keep]


def _route(points):
    return dict(kind='rover_path', points=_sample(points, MAX_ROUTE_POINTS)) if points else None


def _current(message, session):
    return isinstance(message, dict) and session is not None and \
        (message.get('session_id'), message.get('map_epoch')) == tuple(session)


def frame_detections(view, session, *, now):
    """The newest detection frame (#55 `app.state.detection_view`) as bounded text facts.

    Only this map's frame counts; the image and pixel boxes never reach the answer model.
    """
    message = view[0] if isinstance(view, tuple) and view else None
    if not _current(message, session):
        return None
    return dict(age_s=_age(now, message.get('t_wall_ms', 0) / 1000), detections=[
        {'class': d.get('class'), 'confidence': d.get('confidence'), 'position_m': d.get('position'),
         'depth_m': d.get('depth_m')} for d in message.get('detections', [])[:MAX_FRAME_DETECTIONS]])


def approach_summary(route, session, *, now):
    """The selected person's suggested walking approach (the last /route response), not the rover's path."""
    if not _current(route, session):
        return None
    summary = dict(kind='suggested_walking_approach_to_person', status=route.get('status'),
                   person_position_m=route.get('person'), start_m=route.get('start'),
                   age_s=_age(now, route.get('t_wall_ms', 0) / 1000), verified=False)
    if route.get('status') == 'ok' and route.get('points'):
        summary.update(approach_m=_sample([route['approach']], 1)[0], length_m=route.get('length_m'),
                       points=_sample(route['points'], MAX_APPROACH_POINTS))
    else:
        summary['reason'] = route.get('reason')
    return summary


def scene_extras(state, session, now):
    """Live evidence beyond stored objects; each part is absent until its producer sets it."""
    extras = dict(latest_frame=frame_detections(getattr(state, 'detection_view', None), session, now=now),
                  approach_route=approach_summary(getattr(state, 'approach_view', None), session, now=now))
    return {key: value for key, value in extras.items() if value} or None


def _extras(source):
    """Optional bounded live evidence (frame detections, selected approach route); absent-safe."""
    if source is None:
        return None
    try:
        value = source()
        if isinstance(value, dict) and value and len(json.dumps(value, separators=(',', ':'))) <= MAX_EXTRAS:
            return value
    except Exception:
        pass
    return None


def grounding(session, objects, events, *, now, live, scout=None, route=None, extras=None) -> dict[str, Any]:
    """The only scene knowledge the answer model receives: stored facts with ages, newest first."""
    names = {o['id']: o['class'] for o in objects}
    context = dict(
        map=dict(session_id=session[0] if session else None, map_epoch=session[1] if session else None,
                 live=bool(live), scout_position_m=scout),
        objects=[{'class': o['class'],
                  **({'label': o['identity']['label']} if o.get('identity', {}).get('status') == 'labeled' else {}),
                  'position_m': o['position'], 'state': o['state'],
                  'last_seen_s_ago': _age(now, o['last_seen']), 'observations': o['observations'],
                  'confidence': o['confidence']} for o in objects[:MAX_CONTEXT_OBJECTS]],
        objects_total=len(objects),
        changes=[dict(kind=e['kind'], **{'class': names.get(e['object_id'], 'unknown')},
                      displacement_m=e['displacement_m'], new_position_m=e['new_position'],
                      s_ago=_age(now, e['t'])) for e in events[-MAX_CONTEXT_CHANGES:][::-1]])
    path = _route(route)
    if path:
        context['route'] = path
    more = _extras(extras)
    if more:
        context['extras'] = more
    return context


def _clean(text, limit):
    if not isinstance(text, str):
        return None
    text = ' '.join(text.split())
    return text if 0 < len(text) <= limit and not any(ord(c) < 32 for c in text) else None


def register_voice_routes(app, providers, budget, evidence):
    """`evidence()` -> dict(session, objects, events, live, scout, route, extras) of the shown map."""
    busy = asyncio.Lock()
    asked = 0

    @app.get('/voice')
    async def voice_status():
        return dict(version=1, status='unavailable' if providers is None else 'ready')

    @app.post('/voice/ask')
    async def voice_ask(request: Request):
        nonlocal asked
        if providers is None:
            raise HTTPException(503, 'Voice Q&A unavailable')
        mime = request.headers.get('content-type', '').split(';')[0].strip().lower()
        if mime not in AUDIO_TYPES:
            raise HTTPException(415, 'Unsupported audio type')
        declared = request.headers.get('content-length')
        if declared and declared.isdigit() and int(declared) > MAX_AUDIO:
            raise HTTPException(413, 'Question audio too long')
        audio = bytearray()
        async for chunk in request.stream():
            audio.extend(chunk)
            if len(audio) > MAX_AUDIO:
                raise HTTPException(413, 'Question audio too long')
        if len(audio) < MIN_AUDIO:
            raise HTTPException(422, 'Question audio too short')
        if busy.locked():
            raise HTTPException(429, 'Voice question in progress; retry later')
        async with busy:
            found = evidence()
            session = found['session']
            if asked >= budget:
                raise HTTPException(429, 'Voice question limit reached; restart the backend to allow more')
            asked += 1
            result = dict(version=1, session_id=session[0] if session else None,
                          map_epoch=session[1] if session else None, status='no_speech', question=None,
                          answer=None, evidence=dict(objects=len(found['objects']), changes=len(found['events'])),
                          speech=None)
            try:
                text = await asyncio.wait_for(providers.transcriber.transcribe(bytes(audio), mime), TRANSCRIBE_S)
            except Exception:
                raise HTTPException(502, 'Speech transcription unavailable') from None
            del audio
            question = _clean(text[:MAX_QUESTION] if isinstance(text, str) else None, MAX_QUESTION)
            if question is None:
                return result
            result['question'] = question
            context = grounding(session, found['objects'], found['events'], now=time.time(), live=found['live'],
                                scout=found.get('scout'), route=found.get('route'), extras=found.get('extras'))
            if await request.is_disconnected():
                raise HTTPException(499, 'Question cancelled')
            try:
                answer = _clean(await asyncio.wait_for(providers.answerer.answer(question, context), ANSWER_S),
                                MAX_ANSWER)
            except Exception:
                answer = None
            if answer is None:
                raise HTTPException(502, 'Answer unavailable')
            result.update(status='ok', answer=answer)
            if await request.is_disconnected():
                raise HTTPException(499, 'Question cancelled')
            try:
                pcm = await asyncio.wait_for(providers.speaker.synthesize(answer, limit=MAX_REPLY_PCM), SPEAK_S)
                wav = wav_audio(pcm, MAX_REPLY_PCM)
                result['speech'] = dict(status='ready', mime='audio/wav', duration_s=len(pcm) / (RATE * 2),
                                        data=base64.b64encode(wav).decode('ascii'))
            except Exception:
                result['speech'] = dict(status='error')
            return result


def scout_position(pose, max_age_s=2.):
    """Phone/scout x,z in scan meters, or None without a fresh, tracked pose."""
    if pose is None or pose.age_s > max_age_s or pose.tracking != 'normal' or \
            not all(math.isfinite(v) for v in (pose.x, pose.z)):
        return None
    return [round(pose.x, 2), round(pose.z, 2)]
