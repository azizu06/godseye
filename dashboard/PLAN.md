# Dashboard implementation plan

**Goal:** Deliver the desktop spatial-memory experience defined in [SPEC.md](SPEC.md).
**Architecture:** A React workspace consumes validated v1 messages through either an in-process simulator or a WebSocket/REST transport.
A bounded state store supplies a Three.js scene and semantic inspection panels.
**Stack:** TypeScript, React, Vite, Three.js, React Three Fiber, Drei, Lucide, Vitest, Playwright.

## Constraints and review focus

- Keep frontend changes in dashboard/ and preserve the frozen shared interface.
- The user subsequently authorized backend integration; narrowly scoped backend transport fixes are covered by issue #14 and reviewed PR #17.
- All coordinates are ARKit Y-up meters; never show synthetic content as real data.
- Faults, source changes, and focus loss must end manual commands.
- Ignore unknown messages; reject invalid known payloads and bound accumulated data.
- Reconnect must not combine unregistered maps; real backend control failures remain visible.
- No cloud dependencies at runtime; no direct hardware integration.

## Tasks

- [x] Protocol and state: tests first for invalid coordinates, occupancy decode, duplicate/bounded chunks, and bounded event/trajectory retention; implement src/protocol.ts and src/state.ts.
- [x] Simulator and transport: tests first for explicit arm, stop/fault, mode change, held command expiry, relocation, and source isolation; implement src/simulator.ts, src/transport.ts, and src/useMission.ts.
- [x] Desktop UI: implement src/App.tsx, src/Scene.tsx, supporting components, and src/styles.css; write browser coverage of the user-facing demo and controls before completion.
- [x] Integration: run the actual tools/fake_live.py feed, verify all v1 types and absence of simulator geometry in external mode, and exercise reconnect and telemetry-only mode.
- [x] Polish and delivery: inspect desktop/narrow screenshots, run unit tests/typecheck/build/browser suite and existing Python tests where available, document verified behavior and remaining API coordination, then commit.

## Execution notes

The user explicitly requested spec creation followed immediately by implementation.
Work proceeds on codex/dashboard-mission-control in the existing clean checkout, scoped to dashboard/.

## Incremental delivery

The initial dashboard PR implements the simulator and frozen v1 transport.
A subsequent PR pulls current main and integrates additive backend session identity, event history, and rescan responses with explicit tests.

The final workspace refinement in issue #16 preserves information components as expandable overlays, adds progressive discovery and turn-first directional steering, and makes the scene fill the viewport by default.
