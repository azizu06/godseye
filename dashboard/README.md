# Godseye dashboard

A desktop spatial-memory workspace with a local simulator and a configurable WebSocket/REST backend connection.
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
The default source is explicitly simulated; open Connection settings to enter your laptop's `/live` WebSocket and REST base URLs.
Telemetry-only mode supports `tools/fake_live.py` without sending commands.
The real car adapter remains logging-only and cannot arm hardware.

## Demo

The map fills the screen; open Spatial Memory, Object Intelligence, or Recent Activity from the floating panel controls.
Objects and point-cloud surfaces appear as the simulator observes them and remain in spatial memory.
Select an object to inspect its position, confidence, observation history, and state.
Open Rover Controls and run the relocation demo to move the simulated backpack and reveal the previous location, displacement, and event evidence.
Use middle-drag to orbit, Shift + middle-drag to pan, the wheel to zoom, or the visible Orbit/Pan tools with the primary mouse button.
The 2D tab provides a top-down map when inspecting occupancy or selecting a navigation goal.
Arm explicitly before moving.
Standard offers both arrow-key steering and click-to-navigate in the 3D scene or 2D map.
Up moves straight along the rover’s current heading immediately. Down turns 180°, and Left/Right turn 90° before moving forward. Each new key press chooses a rover-relative heading; holding the key or orbiting the camera does not change that target.
Release the key to stop steering.
Focus loss ends held commands, while STOP disarms the rover.
Explore and navigation are simulated and do not imply hardware support.

## Verify

```sh
npm test
npm run build
npm run format:check
npx playwright install chromium
npm run test:e2e
```

The browser suite uses Chromium with software WebGL and one worker for repeatability.
For the real Python integration cases, start the backend on port 8765 and `tools/fake_live.py --port 8766`, then run `GODSEYE_INTEGRATION=1 npm run test:e2e`.
Linux needs Chromium's system libraries; `npx playwright install --with-deps chromium` can install them on a suitable development host.

## Boundaries

Received point chunks, trajectory samples, and event history are bounded in memory.
Unknown message types are ignored and malformed known messages are rejected.
A reconnect clears spatial data before receiving a new snapshot.
Backend map identity changes also clear spatial data; enabled API connections hydrate matching saved events and merge them with live events.
Rescan saves a baseline and watches new observations; it does not command a physical revisit.
Exports contain the dashboard's bounded received state, not a complete backend session recording.
Live camera calibration, hardware drive behavior, and session lifecycle extensions require coordination with their owning components.

### Color surface reconstruction

3D now defaults to solid observed surfaces. Scene layers keeps Point cloud available as an independent overlay. The simulator reveals warm-colored room surfaces progressively. Live rendering prefers native JPEG and raw RGB-D from `/capture/rich/frame.bin`, falling back to the calibrated v1 `/capture/frame.bin` bundle. Set Backend API base even if drive commands are disabled.

Only valid, high-confidence, observed surfaces are drawn. Holes and incomplete scans remain visible; point-only feeds cannot provide textured surfaces. Native camera color is preserved, with textures capped at 1280 pixels on their longest side and a bounded view cache. This is live RGB-D reconstruction, not a watertight or photogrammetry-quality model. See [SURFACES.md](SURFACES.md) for calibration, limits and lifecycle.
