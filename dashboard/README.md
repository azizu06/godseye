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

Select an object to inspect its position, confidence, observation history, and state.
Run the relocation demo to move the simulated backpack and reveal the previous location, displacement, and event evidence.
Use middle-drag to orbit, Shift + middle-drag to pan, the wheel to zoom, or the visible Orbit/Pan tools with the primary mouse button.
The 2D tab provides a top-down map when inspecting occupancy or selecting a navigation goal.
Arm explicitly before moving; hold a direction button to drive and release to stop.
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
