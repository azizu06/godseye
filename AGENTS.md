# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Frozen wire contract: `docs/INTERFACES.md`; use it over the preliminary spec.
- Backend run/test commands and safety limitations: `backend/README.md`.
- The default car adapter is logging-only and reports the car down; never imply this backend can arm or drive hardware. Adapter seam, command generation/lease rules, per-arm session/seq Command/Stop envelopes and the navigation `submit` hook: `backend/README.md` (Drive commands).

- Synthetic phone/live sources and offline validation commands: `tools/README.md`. Hardware-free car-ready regression smoke: `python -m tools.car_smoke`; deterministic real-packet driving comparison/replay: `python -m tools.scout_benchmark` (see tools/README.md).
- Offline nav-log planner replay for teammates without rover access (`python -m tools.replay_nav_log`, hardware-free): `tools/README.md` (Offline nav-log replay). Recorded nav-log runs live at gitignored `backend/captures/nav-logs/<run>/`; the tool replays recorded pose/occupancy only, never sensor/motor physics.

- Live `points`, `objects`, `detections` and `event` payloads, limits, object memory, the same-frame detection image and rescan/change-evidence rules, and hand-off/smoke checks: `backend/README.md` (Live map points, Live objects, Live detection overlay, Suggested approach route, Rescan and change events).
- Opt-in Gemini crop labels, persisted identity statuses and saved-object `/ask` search: `backend/README.md` (Gemini crop labels and saved-object search); fake-only acceptance: `python -m unittest backend.tests.test_labels -v`.
- Bounded Explore stationary RGB-D checkpoints and their quality limitations: `backend/SCAN_PACING.md`.
- `/goal`/explore planning, follow loop, stop reasons and the unverified yaw-sign/mount/turn-in-place car dependencies: `backend/README.md` (Navigation).
- Detection/parser integration and the hardware-free accuracy test command: `backend/DETECTION.md`.
- Motion readiness needs a measured rover calibration (`GODSEYE_ROVER_CALIBRATION`); its navigation map handle is `app.state.map_snapshot()`. See `backend/README.md` (Rover calibration and the navigation map). Never add default rover dimensions.

- Viewer display freshness, feed URL selection, dense points, compact previews, coverage retirement, shared relay, incremental rendering, the 3D object-label evidence/declutter policy, retained last-measured people (removed only by fresh-depth proof) and floor-relative overlays: `dashboard/VIEWER.md`; browser regressions: `dashboard/tests/viewer.spec.ts`, `dashboard/tests/hallwayRender.spec.ts`.
- Persistent colored surface map budgets, block-local capacity recovery and its saturation regression: `dashboard/PERSISTENT_SCAN.md` (Capacity recovery). Browser tests reuse an existing server on 5173; set `GODSEYE_DASHBOARD_TEST_PORT` to avoid a running demo.

- iPhone setup and Swift/backend validation: `ios/README.md`. Separate full-sensor v2 upload, archive format, storage limits and capture inspection: `docs/CAPTURE.md`.
- Direct iPhone ELEGOO V4 manual control, timed PWM protocol and hardware-free loopback validation: `ios/ROVER.md`. This does not enable the backend's navigation adapter.
- Autonomous rover integration status, measured actuation profiles and ESP permit/session protocol: `docs/AUTONOMY.md`. Never substitute test calibration values for real rover measurements. The explicitly requested uncalibrated prototype uses operator-supplied estimates and documented PWM levels; see the prototype option there.

- Offline spoken change-event playback, provider approval gate and deterministic demo: `backend/AUDIO.md`.
- Click-to-talk voice Q&A (ElevenLabs STT -> grounded Gemini -> ElevenLabs TTS), grounding bounds, `voice_extras` seam, typed dashboard `actions` (allowlist) and live opt-in: `backend/VOICE.md`.
- Voice rover suggestions (destination/explore/bounded move) become single-use confirmation cards over `/goal`, explore or one pose-measured Standard move (never timed), confirmed by click or a spoken "go" on the one live card, never direct actuation; a spoken stop runs the operator Stop at once; manual-move readiness is separate from autonomy/map readiness: `backend/NAV_ACTIONS.md`.
- Playwright reuses any server on its port (default 5173, often a live demo): set `GODSEYE_DASHBOARD_TEST_PORT` to a free port.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
