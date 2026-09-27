# God's Eye Interfaces (v1, optional floor-frame v2)

Frozen contract between the iPhone app, the Mac backend, the dashboard, and the car.
Change it only by agreement, and bump `version` when you do. Based on the v0.1 spec, section 13.

## Network

- All devices join one phone hotspot, 2.4 GHz ("Maximize Compatibility" on).
- The Mac backend listens on port **8765**. Put the Mac's hotspot IP in each client's config; don't hardcode it.
- Existing messages carry `"version": 1`. A frame carrying a classified ARKit
  floor plane uses `"version": 2`; its binary layout and all v1 fields stay the same.

## Coordinates and units (v1)

- **Use the ARKit world frame everywhere for now**: meters, right-handed, **+Y up**, origin where the AR session started.
  This matches Three.js (also Y up), so the dashboard can draw positions directly.
- The floor is the X–Z plane. The 2D occupancy grid uses (x, z).
- 4×4 transforms are sent as **16 floats, column-major** (the same memory order as `simd_float4x4`).
- The spec's separate rover base frame (x forward, z up) comes later, once the phone mount is calibrated.
  Until then, the rover's position is the camera position projected onto the floor.

## Timestamps

- `t_capture`: `ARFrame.timestamp` in seconds (phone uptime clock). Use this to match pose, image and depth.
- `t_wall_ms`: phone wall-clock milliseconds when sent. Used only for latency and staleness checks.

---

## 1. iPhone → Mac: `ws://<mac>:8765/phone`

### 1a. `hello` (JSON text, once per connection)

```json
{ "version": 1, "type": "hello", "device": "iphone-17-pro", "session_id": "uuid",
  "map_epoch": 1, "supports_scene_depth": true, "supports_mesh": true }
```

A new or reset AR session gets a new `session_id` and increments `map_epoch`. The backend never mixes data across epochs.

### 1b. `pose` (JSON text, 30 Hz)

```json
{ "version": 1, "type": "pose", "session_id": "uuid", "map_epoch": 1, "frame_id": 1842,
  "t_capture": 5123.4412, "t_wall_ms": 1790380848123,
  "transform": [16 floats, column-major, camera to world],
  "tracking": "normal" }
```

`tracking` is one of `normal`, `limited`, `not_available`. The backend stops the car on anything other than `normal`.

### 1c. `frame` bundle (binary, 5–10 Hz)

One binary WebSocket message, laid out as:

```
[uint32 LE: header_len][header JSON, UTF-8][JPEG bytes][depth bytes][confidence bytes]
```

Header JSON:

```json
{ "version": 1, "type": "frame", "session_id": "uuid", "map_epoch": 1, "frame_id": 1842,
  "t_capture": 5123.4412, "t_wall_ms": 1790380848123,
  "transform": [16 floats, column-major, camera to world, from the SAME ARFrame],
  "tracking": "normal",
  "image": { "width": 960, "height": 720, "jpeg_len": 81234,
             "intrinsics": [fx, 0, 0, 0, fy, 0, cx, cy, 1],
             "orientation": "landscape_right" },
  "depth": { "width": 256, "height": 192, "format": "float32_m", "len": 196608 },
  "confidence": { "width": 256, "height": 192, "format": "uint8_0_2", "len": 49152 } }
```

- **Image**: `ARFrame.capturedImage` downscaled to 960×720 and JPEG-encoded (quality around 0.6). Send it in the sensor's native orientation (landscape right), not rotated to match the screen.
- **Intrinsics**: 3×3 column-major, **scaled to the JPEG you send**. Log the real `capturedImage` size on the phone first (often 1920×1440), then multiply fx and cx by `jpeg_width / native_width`, and fy and cy by `jpeg_height / native_height`.
- **Depth**: `sceneDepth.depthMap`, float32 meters, little-endian, row-major.
- **Confidence**: `sceneDepth.confidenceMap`, uint8 (0 = low, 1 = medium, 2 = high).
- Never send pose from a different frame than the image and depth in the same bundle.

An iPhone frame with an ARKit plane anchor classified **floor** may add a `floor`
object and set only that frame's `version` to 2:

```json
"floor": { "y": -0.16, "polygon": [[-1.0, -1.0], [1.0, -1.0], [1.0, 2.0], [-1.0, 2.0]] }
```

`y` is the plane's ARKit world height; `polygon` is 3–64 boundary vertices in
world X–Z meters from the same ARFrame's anchor snapshot. The plane must be
5 cm–1.5 m below the current camera, cover at least 0.04 m², and fit within
20 m per side, with its X–Z bounds overlapping the camera's local 6 m window.
A v2 frame without valid floor evidence is rejected. A v1 frame
cannot include `floor`. This seeds **free floor cells only** where an ARKit floor
anchor exists; it does not manufacture obstacle depth for reflective objects.
Navigation still requires the profile's minimum accepted depth samples; a floor
polygon alone cannot refresh collision sensing.

### 1d. `mesh` (optional, P2)

Leave it out of v1. Add it with its own message type when the point cloud works.

---

## 2. Mac → dashboard: `ws://<mac>:8765/live`

JSON text messages, each with a `type`. The dashboard ignores types it doesn't know.

| type | rate | payload |
|---|---|---|
| `health` | 2 Hz | `{ phone, car, detector: "ok"/"stale"/"down", pose_age_ms, mode, armed, stop_reason }` |
| `pose` | 15 Hz | `{ position: [x,y,z], yaw_rad, tracking }` |
| `points` | 2–5 Hz | `{ chunk_id, positions: [x,y,z,...], colors: [r,g,b,...] }`, downsampled; the dashboard appends chunks and caps the total |
| `occupancy` | 1 Hz | `{ origin: [x,z], cell_m: 0.05, width, height, cells: "base64 uint8" }`, where 0 = unknown, 1 = free, 2 = occupied |
| `path` | on change | `{ points: [[x,z], ...] }` |
| `objects` | on change | `{ objects: [{ id, class, position: [x,y,z], confidence, first_seen, last_seen, observations, state }] }` |
| `event` | on change | `{ kind: "new"/"moved"/"possible_move"/"not_found", object_id, old_position, new_position, displacement_m, t }` |
| `detections` | 2 Hz | `{ frame_id, t_capture, t_wall_ms, image: {width, height}, source: "backend_detector", classes, detections: [{ class, confidence, box: [x1,y1,x2,y2], position: [x,y,z] or null, depth_m, object_id }] }`, additive; box in that frame's JPEG pixels, `position` only with same-frame depth. Frame JPEG: `GET /capture/detections.jpg` (see `backend/README.md`) |

`state` is one of `present`, `last_seen`, `moved`, `not_found_on_rescan`.

`health` optionally includes `navigation_wait_reason`: `null` while navigation is
not waiting, or an active Explore zero-motion reason such as `no_feasible_step`,
`path_blocked`, `start_blocked`, `no_path`, `search_limit`, `explore_complete`, or
the prototype sensor/relay pause reason. This does not disarm or grant motion
authority. `no_feasible_step` retains the safe planned route but has no supported
clear pursuit command; identical replans do not retry it until map, route or pose
evidence changes. Terminal stops still use `stop_reason` and clear this wait reason.

`health` optionally includes `mission_entry` (also in `GET /health` and arm responses):
`null`, or `{ session_id, map_epoch, start: [x,z], frame_id, t_capture, started_at_ms,
basis: "explore_start" }`. It is the measured camera-floor projection at the first
successful Explore arm, fixed before Explore can move. `frame_id` and `t_capture`
identify that accepted phone pose; `started_at_ms` is backend wall time in milliseconds.
Pose freshness follows the active backend readiness profile (including the explicit
prototype profile), with no separate entry-only age threshold.
The nested session/epoch must match the viewer's active source/map before use.
Missing, null, malformed or mismatched entries are unavailable, never a fallback to
current position or the AR origin. The entry persists across Stop/rearm and same-map
reconnect/restart; a new session/epoch has none until its first successful Explore.
Legacy/restarted sessions without a recorded entry are not backfilled: start a new
session at the intended entry point. This metadata changes no motion authority or
walking-clearance assumptions; `/route` remains the existing visualization-only API.


## 3. Dashboard → Mac: REST on `http://<mac>:8765`

| Method | Path | Body | Does |
|---|---|---|---|
| POST | `/session` | none | New map session |
| POST | `/arm` | none | Arm, only if health is all `ok` |
| POST | `/stop` | none | Latch the stop state (always accepted) |
| GET | `/autonomy` | none | Readiness, active arm generation, exploration counters, yield/wait reason and prototype power ceiling |
| POST | `/explore/yield` | `{ "generation": 1, "reason": "person_path_crossing" / "person_clearance_unknown" / "obstacle_wait" }` | Authenticated in-session zero-motion hold; requires armed Explore and matching generation |
| POST | `/explore/resume` | `{ "generation": 1 }` | Authenticated release after clearance evidence and current readiness; replans in the same generation, cannot arm or release a Stop/fault |
| POST | `/mode` | `{ "mode": "manual" / "navigate" / "explore" }` | Stops first, then switches |
| POST | `/manual` | `{ "v_mps": 0.1, "yaw_rate_rps": 0.0 }` | Held-button driving. The dashboard resends every 100 ms; the car stops if these stop arriving |
| POST | `/goal` | `{ "x": 1.2, "z": -0.8 }` | Drive to a clicked point |
| POST | `/rescan` | none | Save a baseline and start the revisit |
| POST | `/route` | `{ "session_id", "map_epoch", "object_id", "start": [x, z], "purpose"?: "selected" / "recon" }` | Suggested walking approach to a recently observed person; unavailable on stale/unsupported or changed evidence. Visualization only, never a goal or motion; history retained (additive, see `backend/README.md`) |
| POST | `/ask` | `{ "question": "where's my backpack?" }` | Answer from saved objects (P2) |
| GET | `/objects`, `/events`, `/health` | none | Current state |

`/route` defaults to `purpose: "selected"`, retaining the selected suggestion for
read-only voice answers. `purpose: "recon"` independently plans each person without
changing the selected request, cache or another recon response. Both purposes
apply identical target, map and lifecycle evidence checks; neither commands motion.
An invalid purpose is rejected with `422`.

## 4. Mac → car

**Phase 1 (tonight):** send the stock ElegooKit Wi-Fi commands, once triumph confirms the format from ELEGOO's official V4 repo.
Write it behind one Python function, `drive(v_mps, yaw_rate_rps)`, so nothing else changes when phase 2 lands.

**Phase 2:** the spec's drive command, sent over WebSocket to the ESP32 bridge at 20 Hz:

```json
{ "version": 1, "session_id": "uuid", "map_epoch": 1, "seq": 184, "mode": "navigate",
  "v_mps": 0.12, "yaw_rate_rps": 0.20, "issued_at_ms": 812340, "valid_for_ms": 250,
  "arm_token": "session-specific-token" }
```

- Speed limits: 0.10–0.15 m/s to start, 0.20 m/s maximum. Turning: 0.5 rad/s maximum.
- The paired iPhone/ESP hackathon adapter brakes after **1 s** without a fresh
  autonomous command; its Uno timer expires after **1.5 s**. Manual control
  retains the shorter 200 ms timer.

## Fake data (so nobody waits)

- **Backend:** `tools/fake_phone.py` replays a recorded session (or random poses) into `/phone`.
- **Dashboard:** `tools/fake_live.py` emits every `/live` message type with made-up objects and a moving pose.
- **iOS:** until the backend is up, test against `websocat -s 8765`.
