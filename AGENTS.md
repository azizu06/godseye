# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Frozen wire contract: `docs/INTERFACES.md`; use it over the preliminary spec.
- Backend run/test commands and safety limitations: `backend/README.md`.
- The drive adapter is logging-only. Never imply this skeleton can arm or drive hardware.

- Synthetic phone/live sources and offline validation commands: `tools/README.md`.

- Live `points`, `objects` and `event` payloads, limits, object memory and rescan/change-evidence rules, and hand-off/smoke checks: `backend/README.md` (Live map points, Live objects, Rescan and change events).
- `/goal`/explore planning, follow loop, stop reasons and the unverified yaw-sign/mount/turn-in-place car dependencies: `backend/README.md` (Navigation).
- Detection/parser integration and the hardware-free accuracy test command: `backend/DETECTION.md`.

- iPhone setup and Swift/backend validation: `ios/README.md`. Separate full-sensor v2 upload, archive format, storage limits and capture inspection: `docs/CAPTURE.md`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
