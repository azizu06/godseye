# Godseye dashboard

A desktop spatial-memory workspace showing observations from a configurable WebSocket/REST backend connection. No scene data is generated locally.
Start with [SPEC.md](SPEC.md), [PLAN.md](PLAN.md), and the shared [wire contract](../docs/INTERFACES.md).
The [sensor profile](SENSOR_PROFILE.md) documents the five-meter LiDAR range and uncalibrated viewing angle.

## Run

Use Node.js 22 or later.

```sh
cd dashboard
npm ci
npm run dev
```

Open http://localhost:5173.
Press Escape to open the workspace, then Connection settings to enter your laptop's `/live` WebSocket and REST base URLs. The default is localhost:8765; REST commands start disabled.
Without backend observations, the workspace remains empty and waiting. See [REAL_DATA.md](REAL_DATA.md) for the current production source policy.
The real car adapter remains logging-only and cannot arm hardware.

## Workspace

The map fills the screen with 2D/3D and Frame scan controls. Standard/Explore and Arm/Stop live in the workspace's Rover Controls panel. Visible diagnostics show the feed address, connection, received point count, displayed triangle count, layer visibility and capture reason.
Press Escape, Shift+F10 or the context-menu key, right-click without dragging, or touch and hold the canvas to open the workspace.
Spatial Memory, Object Intelligence, Recent Activity, Rover Controls, Scene settings, connection, export and session actions remain there.
Keyboard navigation also reveals an Open workspace launcher on focus.
Objects and surfaces appear only when received from the backend and remain in spatial memory.
Select an object to inspect its position, confidence, observation history, and state.
Open Rover Controls to save a rescan baseline; subsequent backend observations provide change evidence.
Left-drag pans, right-drag rotates, and the wheel zooms.
Middle-drag also orbits; Shift + middle-drag pans, Home resets the view, and F or Frame scan fits received geometry. The first geometry in a map is framed automatically.
A stationary left-click still navigates; a stationary right-click opens the workspace.
The 2D tab provides a top-down map when inspecting occupancy or selecting a navigation goal.
Arm explicitly before moving.
Standard offers both arrow-key steering and click-to-navigate in the 3D scene or 2D map.
Up moves straight along the rover’s current heading immediately. Down turns 180°, and Left/Right turn 90° before moving forward. Each new key press chooses a rover-relative heading; holding the key or orbiting the camera does not change that target.
Release the key to stop steering.
Focus loss ends held commands, while STOP disarms the rover.
Explore and navigation depend on the connected backend and its readiness checks.

## Verify

```sh
npm test
npm run build
npm run format:check
npx playwright install chromium
npm run test:e2e
```

The browser suite uses isolated, deterministic external protocol fixtures, Chromium with software WebGL, and one worker for repeatability. Test fixtures are never imported by the production app.
For the real Python integration cases, start the backend on port 8765 and `tools/fake_live.py --port 8766`, then run `GODSEYE_INTEGRATION=1 npm run test:e2e`.
Linux needs Chromium's system libraries; `npx playwright install --with-deps chromium` can install them on a suitable development host.

## Boundaries

Received point chunks, trajectory samples, and event history are bounded in memory.
Unknown message types are ignored and malformed known messages are rejected.
A reconnect preserves the accumulated colored map and spatial history while pausing capture until the backend confirms the same session/epoch. Live pose and health are cleared so retained geometry cannot imply current tracking.
Backend map identity changes also clear spatial data; enabled API connections hydrate matching saved events and merge them with live events.
Rescan saves a baseline and watches new observations; it does not command a physical revisit.
Exports include the accumulated observed colored geometry and bounded received state, not a complete backend session recording. Export before browser reload; geometry recovery and archive replay are not implemented. Feed addresses survive reload through the page URL; command permission always resets off.
Live camera calibration, hardware drive behavior, and session lifecycle extensions require coordination with their owning components.

### Color surface reconstruction

3D defaults to observed surfaces plus received `/live` points. Scene layers keeps Point cloud available as an independent overlay. Live rendering prefers native JPEG and raw RGB-D from `/capture/rich/frame.bin`, falling back to the calibrated v1 `/capture/frame.bin` bundle. Set Backend API base even if drive commands are disabled. See [VIEWER.md](VIEWER.md) for display freshness, URL selection and rendering limits.

Only valid, medium- or high-confidence, observed surfaces are drawn. Holes and incomplete scans remain visible; point-only feeds cannot provide textured surfaces. Native camera color is baked into an accumulated map, with recent textures (up to 24 views / 48 MiB, at most 1280 pixels on their longest side) supplying fine detail. The map coarsens spatially at its 1,000,000-triangle / 500,000-vertex budget instead of expiring old views. If disconnected geometry cannot fit, capture reports capacity and retains the existing map. The two-million-point cache combines received `/live` points with calibrated native RGB-D measurements. Accepted compressed triangles remove covered dots from the GPU draw list; disabling surfaces reveals the point cache again. See [VIEWER.md](VIEWER.md) for compact transfers, the shared relay and incremental rendering. This is live RGB-D reconstruction, not a watertight or photogrammetry-quality model. See [SURFACES.md](SURFACES.md) and [PERSISTENT_SCAN.md](PERSISTENT_SCAN.md) for calibration, limits and lifecycle.

Dense observed planar regions are simplified before accumulation using normal agreement, an 8 mm plane-fit residual limit, preserved polygon boundaries/holes, and a 0.04 total linear-color error budget.
Uniform regions and smooth color gradients can release interior vertices; uncertain geometry and sharp color detail retain their observed mesh.
This is a conservative geometric approximation, not a semantic wall detector; see [PERSISTENT_SCAN.md](PERSISTENT_SCAN.md).

The coarse preview favors coverage speed: live RGB-D uses up to 3,072 sampled vertices and measured medium/high-confidence depth.
Every skipped native depth sample still supports its coarse cell; missing readings and depth breaks leave gaps instead of guessed wall coverage.
This changes display detail and fusion workload, not the phone's physical capture rate or 5 m range.
