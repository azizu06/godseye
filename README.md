# God's Eye

A hackathon project that pairs a small rover with an iPhone to navigate its surroundings and build a live 3D view of the world on a laptop.

The idea is simple: build a car/rover, connect an Arduino Uno R3 to control its movement, and mount an iPhone at the front. The phone supplies high-quality cameras, LiDAR depth sensing, and on-device processing to support autonomous driving and navigation. At the same time, it streams sensor data to a laptop, where a 3D reconstruction grows and updates as the rover discovers new areas or observes changes.

## Planned hardware

| Component | Role |
| --- | --- |
| Car/rover chassis, motors, and battery | The mobile platform that explores the environment. |
| Arduino Uno R3 and a suitable motor driver | Control the rover's motors in response to movement commands. |
| iPhone 17 Pro or another LiDAR-equipped iPhone | Capture images, depth, and camera pose; process sensor data on the phone. |
| Laptop | Receive the phone's data, build the environment map, and display the live 3D reconstruction. |

Mount the phone with its rear cameras and LiDAR facing forward. The [iPhone 17 Pro includes LiDAR](https://www.apple.com/ie/iphone-17-pro/specs/); the [standard iPhone 17 does not](https://www.apple.com/iphone-17/specs/), so the exact phone can vary as long as it supports the required depth sensing.

## How it will work

1. **See:** The phone captures camera images, depth, and its position and orientation as the rover moves.
2. **Navigate:** The system uses those observations to understand nearby space, avoid obstacles, and plan movement. The Arduino executes motor commands through the motor driver.
3. **Stream:** The phone simultaneously sends synchronized sensor data to the laptop over a local network.
4. **Reconstruct:** The laptop combines incoming observations into a live 3D representation, adding newly discovered areas and updating previously observed ones.

The hackathon demo goal is a rover that navigates a small space while a laptop shows that same space being reconstructed and updated in real time.

## Current status

This repository is an early software prototype. Autonomous navigation and live 3D reconstruction are planned capabilities.

- The Python backend accepts phone pose and frame data and streams health, pose, a live 3D `points` cloud from phone depth, and session-scoped object snapshots when a detector is configured.
- Synthetic phone and live-view data tools support development without hardware.
- A native iPhone capture app streams camera, LiDAR depth, confidence, and pose, and can record richer ARKit data locally. Physical-device validation is still required.
- The 3D dashboard, occupancy mapping, navigation, and Arduino integration still need to be built and connected. The live point handoff has only been checked with synthetic phone data.

**The current drive adapter only logs commands. This prototype cannot arm or drive hardware.** The Arduino control link remains to be implemented.

## Local quick start

Use Python 3.10+ and run from the repository root. This demo needs no phone, car, credentials, or model weights.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements-test.txt -r tools/requirements.txt numpy
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8765 --ws-max-size 8388608
```

In another terminal, send synthetic phone data:

```sh
.venv/bin/python tools/fake_phone.py --url ws://127.0.0.1:8765/phone
```

Inspect health at <http://127.0.0.1:8765/health> or REST schemas at <http://127.0.0.1:8765/docs>. The backend reports phone health/pose and streams `points` chunks on `/live` (`python tools/probe_live.py` prints them); object and path snapshots are empty. It has no authentication, so keep this demo local. Stop each process with Ctrl-C.

Run the hardware-free tests:

```sh
.venv/bin/python -m unittest discover -s backend/tests
.venv/bin/python -m unittest discover -s tools/tests
```

## Development

- [iPhone app setup, captured data, recording format, and validation](ios/README.md)
- [Backend setup, running, and tests](backend/README.md)
- [Synthetic data tools and offline validation](tools/README.md)
- [Detection and localization integration](backend/DETECTION.md)
- [Frozen v1 interface contract](docs/INTERFACES.md) — the authority for existing message formats and coordinates.
- [Preliminary technical spec](docs/spec-v0.1.md) — earlier design context; the interface contract takes precedence.

The interface contract describes intended integration; the backend and tools docs describe what works today
