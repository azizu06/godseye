# Voice Q&A (push-to-talk)

Ask Scout a spoken question about what it has observed and hear a spoken answer.
Off by default: without configured providers `GET /voice` reports
`{"version":1,"status":"unavailable"}`, `POST /voice/ask` returns 503 before reading
audio, and the dashboard shows "Voice Q&A unavailable on this backend." This is
question answering only. It never arms, drives or steers the car, certifies an area
as clear, recognizes people, or invents routes or destinations.

## Data flow

1. The dashboard's **Hold to ask Scout** button (bottom right; external backend with
   REST commands enabled) opens the microphone only while it is held, for at most
   15 seconds, then stops every track. Clips shorter than 0.4 s are discarded
   locally. Cancel/Stop aborts the request or stops playback.
2. The browser posts the raw `MediaRecorder` clip (`audio/webm`, `ogg`, `mp4`,
   `mpeg`, `wav` or `aac`; 1 KB to 2 MB) as the body of `POST /voice/ask`. One
   question is in flight per backend (others get 429). A backend process allows
   100 questions in total (`create_app(voice_budget=...)`); a new map does not
   reset it. Further questions get 429 until restart.
3. The backend sends the clip to ElevenLabs
   [speech-to-text](https://elevenlabs.io/docs/api-reference/speech-to-text/convert)
   (`scribe_v2`, multipart, `xi-api-key`). An empty transcript returns
   `status: "no_speech"` without further calls.
4. `voice.grounding()` builds the only scene knowledge Gemini receives, from the
   shown map (active, else newest stored): up to 40 newest objects (class, Gemini
   crop label only when `labeled`, position in scan meters, state, observations,
   confidence, seconds since last seen), the 20 newest change events, whether the
   phone is live, the fresh tracked scout position, and the navigation path when
   one is being followed. Stored ids and images are not sent.
5. `GeminiLabels.answer()` calls `generateContent` with a system instruction that
   limits answers to that JSON, requires last-seen wording instead of presence
   claims, forbids all-clear, person identification, invented routes and movement
   commands, and treats the question as data. The reply must be JSON
   `{"answer": string}`, at most 600 characters.
6. ElevenLabs text-to-speech (existing `ElevenLabsProvider.synthesize`, PCM 16 kHz,
   at most 30 s) speaks the answer. If speech fails the text answer still returns
   with `speech: {"status":"error"}`.

Response: `{version, session_id, map_epoch, status, question, answer, evidence:
{objects, changes}, speech: {status, mime, duration_s, data(base64 WAV)} | null}`.
Provider failures return sanitized 502s (`Speech transcription unavailable`,
`Answer unavailable`). If the client disconnects, remaining paid stages are skipped.

Nothing is retained: no clip, transcript, answer or reply audio is written to
SQLite, captures or logs. Provider errors and bodies are never logged or returned.

## Future evidence seam

A detection or route producer can set `app.state.voice_extras` to a callable that
returns a small JSON dict (for example `{"detections": [...], "approach_route":
{...}}`). It is added to the grounding as `extras` only when it serializes to at
most 4 KB; missing, failing or oversized extras are omitted.

## Live configuration (owner approval required)

Not exercised. Before a live test, get approval for spend, sending spoken questions
to ElevenLabs, sending observation text to Google, and credentials. Then start the
backend with local environment variables (never commit or print them):

```sh
GODSEYE_VOICE_ENABLED=1 ELEVENLABS_API_KEY=... GODSEYE_ELEVENLABS_VOICE_ID=... \
GEMINI_API_KEY=... GODSEYE_GEMINI_MODEL=... \
$HOME/.venvs/godseye/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8765 --ws-max-size 8388608
```

Optional: `GODSEYE_ELEVENLABS_STT_MODEL` (default `scribe_v2`) and
`GODSEYE_ELEVENLABS_TTS_MODEL` (default `eleven_flash_v2_5`). Keys alone never
enable calls; `GODSEYE_VOICE_ENABLED=1` with a missing key, voice or model fails
startup. This does not enable Gemini crop labels or spoken change events. The
browser needs microphone permission, which requires `localhost` or HTTPS.
A first live run must verify transcription accuracy, answer grounding, voice,
latency and account format support; the offline tests below cannot.

```sh
$HOME/.venvs/godseye/bin/python -m unittest backend.tests.test_voice -v
cd dashboard && npx vitest run src/VoiceAsk.test.ts && npx playwright test tests/voice.spec.ts
```

The tests use synthetic audio bytes, fake providers and `httpx.MockTransport`, and a
fake microphone/recorder/player in the browser. No paid call, real microphone,
recording upload or car hardware is used.
