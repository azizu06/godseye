# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Frozen wire contract: `docs/INTERFACES.md`; use it over the preliminary spec.
- Backend run/test commands and safety limitations: `backend/README.md`.
- The default car adapter is logging-only and reports the car down; never imply this backend can arm or drive hardware. Adapter seam, command generation/lease rules, per-arm session/seq Command/Stop envelopes and the navigation `submit` hook: `backend/README.md` (Drive commands).

- Synthetic phone/live sources and offline validation commands: `tools/README.md`. Hardware-free car-ready regression smoke: `python -m tools.car_smoke` (see Car-ready smoke there).

- Live `points`, `objects` and `event` payloads, limits, object memory and rescan/change-evidence rules, and hand-off/smoke checks: `backend/README.md` (Live map points, Live objects, Rescan and change events).
- Opt-in Gemini crop labels, persisted identity statuses and saved-object `/ask` search: `backend/README.md` (Gemini crop labels and saved-object search); fake-only acceptance: `python -m unittest backend.tests.test_labels -v`.
- `/goal`/explore planning, follow loop, stop reasons and the unverified yaw-sign/mount/turn-in-place car dependencies: `backend/README.md` (Navigation).
- Detection/parser integration and the hardware-free accuracy test command: `backend/DETECTION.md`.
- Motion readiness needs a measured rover calibration (`GODSEYE_ROVER_CALIBRATION`); its navigation map handle is `app.state.map_snapshot()`. See `backend/README.md` (Rover calibration and the navigation map). Never add default rover dimensions.

- Viewer display freshness, feed URL selection, dense points, compact previews, coverage retirement, shared relay and incremental rendering: `dashboard/VIEWER.md`; browser regression: `dashboard/tests/viewer.spec.ts`.

- iPhone setup and Swift/backend validation: `ios/README.md`. Separate full-sensor v2 upload, archive format, storage limits and capture inspection: `docs/CAPTURE.md`.

- Offline spoken change-event playback, provider approval gate and deterministic demo: `backend/AUDIO.md`.
- Push-to-talk voice Q&A (ElevenLabs STT -> grounded Gemini -> ElevenLabs TTS), grounding bounds, `voice_extras` seam and live opt-in: `backend/VOICE.md`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
