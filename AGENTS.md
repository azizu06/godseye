# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Frozen wire contract: `docs/INTERFACES.md`; use it over the preliminary spec.
- Backend run/test commands and safety limitations: `backend/README.md`.
- The drive adapter is logging-only. Never imply this skeleton can arm or drive hardware.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
