# God's Eye desktop dashboard specification

## Mandate and authority

Build a polished desktop browser workspace for the ShellHacks indoor mapping rover.
The user authorized a comprehensive spec, a simulator, and implementation on September 26, 2026.
Optimize for a compelling end-to-end demonstration, visual clarity, and dependable interaction.
This specification covers the dashboard, not physical actuation or backend perception implementation. [REAL_DATA.md](REAL_DATA.md) supersedes historical simulator requirements: production displays only received backend observations.

Read these sources whenever intent or integration is uncertain:

- [Project agent contract](../AGENTS.md): project knowledge and frozen-contract precedence.
- [Preliminary product specification](../docs/spec-v0.1.md): product goals, spatial memory, demonstration, uncertainty, and scope priorities.
- [Frozen v1 interfaces](../docs/INTERFACES.md): authoritative message shapes, coordinates, endpoints, and control semantics.
- [Dashboard issue #5](https://github.com/azizu06/godseye/issues/5): frontend ownership and first working handoff.
- [Backend implementation status](../backend/README.md): actual endpoint support and logging-only hardware boundary.
- [Synthetic sources](../tools/README.md): external fake-live feed, explicit labeling, and test commands.
- [Detection integration](../backend/DETECTION.md): localization meaning and confidence limitations.
- [Sponsor challenges](../docs/sponsor-challenges.txt): context only; no cloud integrations or paid calls required.

The frozen interfaces take precedence over preliminary examples.
Do not alter the shared wire contract without team agreement.
API extensions listed below are deliberately deferred for coordination.

## Product story

God's Eye gives a room a memory.
An operator should immediately understand what was observed, where it is, when it was seen, and what changed.
The strongest demonstration is a distinctive backpack observed at one position, moved while the rover is stopped, and rediscovered with old and new positions visible together.
Prioritize this story ahead of unrestricted exploration.

## Visual direction

Create an instrument-like mission workspace with a near-black navy background, layered slate panels, fine borders, off-white typography, cyan spatial data, and amber change evidence.
Use generous scene space and restrained accents; reserve red for the stop control and faults.
Pair clean sans-serif interface text with monospace telemetry.
Keep the scene legible and immersive using a perspective floor grid, sampled colored room geometry, object markers, a rover heading, and trajectory.
Avoid decorative charts, fictitious telemetry, excessive precision, and unsupported battery or coverage claims.
All states have text labels in addition to color.
Use locally bundled fonts, CSS, vector icons, and received geometry so the workspace requires no external presentation assets.

## Layout and navigation

Target 1440×900 and larger desktop displays; remain usable at 1024×768 and narrow widths.
The spatial view fills the viewport with a floating panel selector for Spatial Memory, Object Intelligence, Recent Activity, and Rover Controls.
A floating action row inside the spatial view groups source selection and Stop beside Export Snapshot and New Session, with no separate brand bar.
Keep these actions accessible while configuring panels.
Information panels are closed by default and expand over the right side of the scene when requested.
The scene header contains 3D/2D selection, layer controls, and reset-view control.
An overlay summarizes actual observed object and point counts and tracking status.
Object selection is synchronized between scene, object list, and event history.
The inspector shows the selected object's class, stable ID, state, confidence, estimated coordinates, observation count, last seen, and locally observed event history.
The bottom area includes the panel selector and compact operator controls.
Spatial Memory provides searchable inventory; Recent Activity provides the event list.
Settings is an accessible dialog with source selection and independently configurable WebSocket and REST addresses.

## Scene and spatial meaning

Use React, TypeScript, Vite, and Three.js with React Three Fiber and Drei.
ARKit coordinates are meters, right-handed, Y up; the floor is X–Z.
Never apply the preliminary base-frame conversion.
The scene renders the actual seven v1 message families: health, pose, points, occupancy, path, objects, and event.
Point chunks are deduplicated by chunk ID and bounded to 500,000 points and 1,000 chunks.
Trajectory is bounded to 600 samples; event history to 200 entries.
Occupancy decodes base64 uint8 cells, validates dimensions, and distinguishes unknown/free/occupied.
Top-down view shares the same spatial data and supports explicit goal placement in Navigate mode.
3D orbit, zoom, reset, and layer visibility controls are functional.
A moved-object selection shows an amber dashed connection between the old and new positions plus displacement.
No room or furniture geometry is invented; only received observations are displayed.
If WebGL fails, provide an actionable fallback and a usable 2D map while preserving Stop and connection controls.

## Data, trust, and connection behavior

Start with an empty scene and the configured backend connection; REST commands are disabled by default.
Never generate fallback data after connection failure.
Connection settings exposes backend addresses and command permission, with no demo or synthetic-source option.
The default backend is localhost:8765.
Show connecting, connected, reconnecting, disconnected, and stale states.
Mark data stale when health is not refreshed for 2 seconds; inhibit ordinary motion controls until fresh telemetry returns.
Reconnect with bounded retry delays and cancel old listeners/timers on backend changes. Preserve received history through same-map reconnection, require identity confirmation before resuming, and clear spatial state when the backend configuration or map identity changes.
Keep real backend health authoritative and separate from transport connection state.
A successful backend /session acknowledgement clears scene state.
Consume the backend's documented optional session_id/map_epoch fields on points, objects, and events to clear spatial state on backend-initiated map changes.

## Two-way commands

Implement POST /session, /arm, /stop, /mode, /manual, /goal, /rescan, and /ask through a shared transport interface.
Expose only supported UI functions; natural-language query remains outside this initial experience.
Surface backend errors, including 409 and 501, in user-readable feedback.
Do not optimistically claim that hardware armed, moved, navigated, or rescanned.
Manual driving uses held controls, sends every 100 ms while enabled, and sends zero on release, pointer cancellation, blur, visibility loss, mode/source change, or unmount.
Allow one outstanding manual request so a slow network does not accumulate motion commands.
Stop bypasses other command pending states, ends manual repeat immediately, and always remains accessible.
User-selected Standard/Explore mode switches stop first and require explicit rearming.
Within an already explicitly armed, healthy Standard session, the same user gesture can hand control between manual and navigation backend modes; a released key, fault, or Stop cancels that authorization.
New session requires an explicit confirmation because it clears the current displayed session.
Export downloads a clearly scoped dashboard snapshot of received data; it is not a full backend session export.
Do not implement any direct car, serial, motor, or vendor command protocol.

## Test fixtures

The former local simulator exists only under automated test fixtures, never in the production app or its source choices.
Deterministic protocol fixtures validate controls, discovery, and fault handling without hardware.
Startup without observations has no generated objects, pose, scope, trajectory, health, or geometry.
Previously received scans remain visible on disconnect, with stale/disconnected status.
See [REAL_DATA.md](REAL_DATA.md) for the current source policy and [progressive discovery](DISCOVERY.md) for observation behavior.

## Deferred API coordination

These are recorded follow-ups, not blockers and not unilateral v1 extensions:

1. A dedicated session-reset/capability contract remains future work; current documented additive session_id/map_epoch fields are supported.
2. Historical observations and evidence references, distinguishing detector confidence, geometric uncertainty, and identity confidence.
3. Backend session export/deletion and replay lifecycle.
4. Capability advertisement for navigation, rescan, query, hardware actuation, and optional imagery.
5. Backend acknowledgement semantics for long-running operations and timestamps/units for all object and event history.

The dashboard hydrates documented GET /events history when an external API is enabled, validates map identity, and deduplicates stable event IDs.
Rescan acknowledges a saved baseline and ongoing observation matching; no completion duration is invented.
The dashboard exports only its local bounded snapshot and treats endpoint errors as authoritative.
See [the backend integration increment](INTEGRATION.md) for implemented support and validation.

## Acceptance criteria

- The app installs, type-checks, and builds with documented commands and pinned lockfile dependencies.
- Startup without a backend remains empty and waiting, with view controls available.
- Users can select/search objects, inspect data, switch 3D/2D, toggle layers, and reset the view.
- Received moved-object events link old/new locations and their reported displacement.
- Controls use the configured backend; Stop, tracking loss, release, and focus loss end held commands.
- Isolated test fixtures exercise all seven message families without production-generated geometry.
- Real backend mode shows its actual disarmed/unavailable state and endpoint errors truthfully.
- Invalid messages, unsupported types, duplicate chunks, oversized payloads, and dropped connections are handled without crashes or unbounded memory.
- Keyboard focus, button labels, dialogs, reduced motion, and usable narrow-screen layout are verified.
- Unit/integration tests cover protocol validation, bounded state, command behavior, and deadman control.
- Browser tests cover empty startup, backend switching, external telemetry, Stop, and error states; screenshots are inspected for layout and rendering defects.

## Historical implementation sequence

The initial sequence below records the original build; the current source behavior is governed by REAL_DATA.md.

1. Commit this spec and the execution plan before product implementation.
2. Build and test typed protocol/state, simulator, and command transport.
3. Build the visual shell, spatial scene, inspector, inventory, activity, settings, and controls.
4. Exercise local simulation and the actual Python fake-live source in a browser; fix visual and behavioral defects.
5. Document setup, evidence, limitations, and deferred coordination; commit the complete dashboard.

## Confirmed navigation refinement

The user explicitly requested Blender-like navigation while implementation was in progress.
Middle-mouse drag orbits; Shift + middle-mouse drag pans; the wheel zooms toward the scene.
Left-mouse drag pans and right-mouse drag rotates; these mappings remain fixed.
A stationary left-click retains navigation, while a stationary right-click opens the workspace.
The minimal canvas and keyboard/touch workspace access follow [DISCOVERY.md](DISCOVERY.md#minimal-canvas); view tools, layers and diagnostics live in Scene settings.
A View controls help popover documents the shortcuts; Home resets the perspective when focus is outside editable fields.
Top-down remains a deliberate view toggle rather than constraining free perspective exploration.

## Confirmed rover viewpoint refinement

Show the rover/phone's tracked location and heading together with a sleek translucent cyan view scope in 3D and 2D.
The v1 pose describes the camera position and heading; ground projection is a rover-position proxy until mount calibration exists.
Use the Apple-documented 5.0 m LiDAR depth limit for the iPhone 17 Pro profile, with a curved radial boundary so corner rays do not overstate distance.
Because /live carries no camera intrinsics or field-of-view value, the angular extent is explicitly uncalibrated, not observed coverage or free space.
See [sensor profile and primary sources](SENSOR_PROFILE.md) before changing these values.
Keep the scope visually distinct from occupancy and planned paths.
Coordinate a calibrated view-frustum payload later with the backend owner.

## Confirmed trail refinement

A subtle trace follows the rover/phone's received positions in 3D and 2D.
Older segments fade in intensity and recent segments remain more visible.
The trace is bounded to 600 meaningful position samples, clears with a session/source reset, and remains distinct from the brighter planned path.

## Full-screen workspace refinement

The spatial view fills the viewport by default.
Preserve Spatial Memory, Object Intelligence, and Recent Activity in expandable panels over the map.
Necessary controls stay within the spatial window, with configuration available on demand.
Standard exposes both keyboard steering and click-to-navigate; Explore is the other UI mode.
Up advances immediately along the rover’s current heading. Down requests a 180° turn, and Left/Right request a 90° turn before advancing. Capture the rover-relative target once per new direction press; camera orbit and held-key repeat must not retarget motion.
Read [DISCOVERY.md](DISCOVERY.md) for the accepted behavior, input handoff, and retention requirements.

## Review and delivery

For this work, the user authorizes merging each increment to main after code review, correction of material findings, and successful relevant checks.
Use the issue-to-PR workflow for each increment and confirm the merged result.

## Observed color surfaces (issue #19)

[SURFACES.md](SURFACES.md) specifies the default 3D representation: solid triangles textured from synchronized camera color and depth. Point cloud remains optional. Prefer native v2 capture, use v1 RGB-D fallback, preserve frozen control/live schemas, and retain accumulated observed geometry beneath a bounded set of recent textured views. Keep unknown areas empty and capture availability explicit.

[PERSISTENT_SCAN.md](PERSISTENT_SCAN.md) specifies accumulated color retention, worker integration, spatial coarsening, reconnect identity, and geometry export. Old observed regions do not expire with recent views. Export before browser reload; persistent storage and archive replay remain future work.
