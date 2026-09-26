# Spoken change events

`create_app(audio_provider=None)` is offline by default: no environment variable,
key, or dashboard action enables ElevenLabs. Existing `/events` and v1 live event
payloads remain unchanged. Only stored change events belonging to the current map
(or newest stored map after restart) can be narrated; arbitrary user text is not
accepted. Templates preserve `possible_move` uncertainty and rescan absence.

- `POST /events/{event_id}/audio` prepares or replays audio metadata:
  `{version, session_id, map_epoch, event_id, text, status, duration_s, audio_url}`.
  Status is `ready`, `unavailable` (no provider), or `error` (sanitized provider
  failure, timeout or invalid output). Missing/out-of-map IDs return 404, invalid
  evidence 422, busy synthesis 429, a full cache 507, and a map changed during
  synthesis 409. No raw provider exception or response body is returned or logged.
- `GET /audio/{sha256}.wav` serves current-map audio only. Names must be exactly
  64 lowercase hex digits: no filesystem paths, downloads, or third-party URLs.
- Providers implement `identity` (non-secret voice/model/format revision) and
  `async synthesize(text) -> bytes` returning mono signed 16-bit little-endian
  16 kHz PCM. Audio is checked before WAV wrapping: nonempty, even byte count,
  maximum 12 seconds/384 KB. Synthesis has a 15-second deadline and one in-flight
  call per backend; excess requests are rejected, not queued.
- SQLite `spoken_audio` caches by session/epoch, stable event ID, templated text
  and provider identity. Restart/reconnect/repeated preparation never resynthesizes
  a cached identity. A failure tombstone is committed before the call so process
  interruption also cannot automatically repeat a paid request. Failures remain
  errors (no automatic retry). Changing the provider revision explicitly creates
  a new identity and may spend again. At 256 total entries, new synthesis stops;
  no eviction silently rebills replay. Audio occupies at most about 98 MB.
  Runtime DBs remain gitignored. Use one backend process per SQLite database.

The dashboard Recent Activity panel offers a saved-event selector, Prepare audio,
and native playback controls when external API actions are enabled. It validates
map/event identity, duration and local audio path, cancels on source/map change,
and clears playback when those change. There is no autoplay or background spend.
The default backend shows Audio unavailable while event evidence remains usable.

## Deterministic offline demo

```sh
$HOME/.venvs/godseye/bin/python -m tools.audio_demo
```

This starts a separate loopback backend on port 8876 with a temporary database,
synthetic rescan evidence of a backpack moving 2 meters, and a **mock tone**
provider. It does not emulate natural speech or count as live ElevenLabs use.
Configure the dashboard external backend/API URL to `http://127.0.0.1:8876` and
WebSocket URL to `ws://127.0.0.1:8876/live`, enable API actions, and open Recent
Activity. Select the saved event, prepare audio and press play. Repeated preparation
returns the same URL. The temporary database disappears when the demo exits.
Without a dashboard: GET `/events`, POST `/events/<id>/audio`, then open the
returned relative `audio_url` on the same backend.

## Live provider gate (not exercised)

`ElevenLabsProvider` follows the official [Create speech API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)
with `output_format=pcm_16000`, `xi-api-key`, and an explicit voice/model. The
adapter streams/caps response bytes, rejects redirects, and does not retry.
HTTP transport tests use a fake key and `httpx.MockTransport` only.

Before real use, obtain **separate spend, real-scene privacy, and credential
approval at the point of need**. An approved operator must inject
`ElevenLabsProvider(api_key, voice_id, spend_approved=True, privacy_approved=True)`
through `create_app(audio_provider=...)`. Do not put credentials in code, browser
settings, logs, provider identity, PR text, or the cache. This task installs no
live-provider startup path and made no paid requests or real-scene uploads.
The first approved live run must verify voice/model availability, account format
support, natural speech quality and latency; offline tests cannot establish them.

```sh
$HOME/.venvs/godseye/bin/python -m unittest backend.tests.test_audio
cd dashboard && npm test -- --run src/SpokenEvent.test.ts
```
