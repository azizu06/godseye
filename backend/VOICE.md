# Voice Q&A (click-to-talk)

Ask Scout a spoken question about what it has observed and hear a spoken answer.
Off by default: without configured providers `GET /voice` reports
`{"version":1,"status":"unavailable"}`, `POST /voice/ask` returns 503 before reading
audio, and the dashboard shows "Voice Q&A unavailable on this backend." This is
question answering only. It never arms, drives or steers the car, certifies an area
as clear, recognizes people, or invents routes or destinations.

## Data flow

1. The dashboard's **Ask Scout** microphone button (bottom right; external backend
   with REST commands enabled) opens the microphone on one click (or Enter/Space)
   and stops it and sends on the next click, or automatically after 15 seconds,
   then stops every track. Clips shorter than 0.4 s are discarded
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
   confidence, seconds since last seen, or null when the phone clock makes it implausible), the 20 newest change events, whether the
   phone is live, the fresh tracked scout position, the rover's own path (`rover_path`)
   when a navigation run follows one, and the live `extras` below. Stored ids and
   images are not sent.
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

## Live frame and approach route

`scene_extras()` adds two optional parts to the grounding as `extras`, each only for
the shown map and absent until its producer sets it:

- `latest_frame`: from `app.state.detection_view` (newest detection frame, PR #55):
  frame age and up to 16 detections as class, confidence, `position_m` (null when
  the box had no reliable depth) and `depth_m`. No image or pixel boxes are sent.
- `approach_route`: from `app.state.approach_view`, the last `/route` response
  (PR #56) plus `t_wall_ms`. It is summarized as the selected person's position, a
  status (`ok` with length, approach point and at most 16 points, or `unavailable`
  with a reason) and age, always `verified: false`. This is a walking suggestion
  and is distinct from the grounding's `route` (the rover's own `rover_path`).

## Live configuration

Put the settings in the gitignored repository-root `.env` (mode 600; never commit,
print or pass keys on the command line) and let uvicorn load it:

```sh
# .env
GODSEYE_VOICE_ENABLED=1
ELEVENLABS_API_KEY=...          # key restricted to Text to Speech, Speech to Text, Voices read
GODSEYE_ELEVENLABS_VOICE_ID=... # e.g. premade "Sarah" EXAVITQu4vr4xnSDxMaL
GEMINI_API_KEY=...
GODSEYE_GEMINI_MODEL=gemini-3.5-flash-lite  # 2.5-flash-lite is closed to new projects
```

```sh
$HOME/.venvs/godseye/bin/python -m uvicorn backend.app:app --env-file .env --host 127.0.0.1 --port 8765 --ws-max-size 8388608
```

Enabling it sends spoken question audio to ElevenLabs and observation text to Google,
and spends provider allowance per question.
Optional: `GODSEYE_ELEVENLABS_STT_MODEL` (default `scribe_v2`) and
`GODSEYE_ELEVENLABS_TTS_MODEL` (default `eleven_flash_v2_5`). Keys alone never
enable calls; `GODSEYE_VOICE_ENABLED=1` with a missing key, voice or model fails
startup. This does not enable Gemini crop labels or spoken change events. The
browser needs microphone permission, which requires `localhost` or HTTPS.
Live smoke (2026-09-26, one synthetic `say`-generated question against a
synthetic moved-backpack map, `scribe_v2` / `gemini-2.5-flash-lite` / Sarah on
`eleven_flash_v2_5`): HTTP 200 in 1.4 s, transcript correct, answer "The backpack
was last seen 20 seconds ago. It has moved 2 meters.", 3.9 s 16 kHz WAV that played.
A follow-up Gemini-only call with a dedicated free-tier "GodsEye Scout" AI Studio
project key and `gemini-3.5-flash-lite` returned a grounded answer in 0.9 s.
A combined local integration with PRs #55 and #56 (plus the proposed
`approach_view` assignment in `/route`) used the real detection message, the real
`/route` planner and all three live providers. It answered "Where is the person, and how
do I get to them?" with "A person was detected four seconds ago. A suggested walking
approach to the person is available with a length of three point four meters." in
1.7 s, with 7.6 s of playable speech. Real rooms, microphones and noisy speech are still untested.

```sh
$HOME/.venvs/godseye/bin/python -m unittest backend.tests.test_voice -v
cd dashboard && npx vitest run src/VoiceAsk.test.ts && npx playwright test tests/voice.spec.ts
```

These tests use synthetic audio bytes, fake providers and `httpx.MockTransport`, and a
fake microphone/recorder/player in the browser; they make no provider calls.
