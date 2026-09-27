# Backend skeleton

The authoritative wire contract is [`../docs/INTERFACES.md`](../docs/INTERFACES.md).
Run from the repository root with the shared local interpreter:

```sh
$HOME/.venvs/godseye/bin/python -m pip install -r backend/requirements-test.txt
$HOME/.venvs/godseye/bin/python -m uvicorn backend.app:app --host 0.0.0.0 --port 8765 --ws-max-size 8388608
$HOME/.venvs/godseye/bin/python -m unittest discover -s backend/tests
```

Use the Mac's hotspot IP, configured in the client, for
`ws://<mac>:8765/phone`, `ws://<mac>:8765/live`, and `http://<mac>:8765`.
REST schemas are available at `/docs`. Use only on the private demo network;
this scaffold has no authentication. CORS permits browser clients without credentials.
`--host 0.0.0.0` listens on IPv4 only. On an IPv6-only iPhone hotspot (carrier CLAT, Mac IPv4 `192.0.0.x`
unreachable) run with `--host ::`, which listens on all interfaces, and point the app at the Mac's Bonjour
name, e.g. `ws://<mac-name>.local:8765/phone` (name from `scutil --get LocalHostName`).

`GODSEYE_DB` selects the SQLite path (default `backend/godseye.db`). Tables
are created at startup from `schema.sql`: sessions, frames, observations,
objects, events, health_events. Session/epoch composite keys isolate phone data.
Frame metadata, safety events, detected objects and their raw observations are
saved; JPEG/depth/confidence bytes are validated for the wire, turned into live
points and objects (below), and never stored in the session database. The latest accepted bundle
is also held in memory for the `/capture` preview. Full v2 capture uploads are recorded separately
as described below; no model weights are produced.
Rescan baselines (`rescans`) and change events (`events`, linked through
`rescan_events`) are saved too; see Rescan and change events.
Runtime databases are gitignored; never commit databases or credentials.

## Transport

`/phone` requires a version-1 `hello` first. One phone owns the connection;
a second is rejected. Reconnect with a new hello when the AR session/epoch changes.
Mismatched session/epoch, malformed bundles, unknown versions, nonfinite values,
and text frames close with code 1008. Binary headers are capped at 64 KiB and
bundles at 8 MiB; dimension/payload lengths must agree. Pose must have 16
column-major floats forming a rigid camera-to-world matrix (zero, scaled, reflected or
non-affine transforms close the socket, like the bundle decoder). Bundle transform belongs
to the same captured frame; a pose and bundle with the same `t_capture` must agree on
frame, transform and tracking or the socket closes.

`/live` sends versioned JSON: health every 500 ms, pose at most 15 Hz,
an objects snapshot and an empty path at connection and map reset.
Map resets discard queued updates from the previous map and immediately publish fresh health, objects and path.
Bounded queues
drop the oldest pending update for slow viewers. No fake scene is emitted.
`points`, `occupancy`, `objects` and `event` are described under Live map points,
Occupancy, Live objects, and Rescan and change events. Position is transform entries 12–14 in ARKit Y-up meters. Yaw
uses camera forward (-Z), measured from world +Z toward +X; mount calibration
and rover base heading remain future work.

Phone freshness uses local monotonic receipt age and requires a wall timestamp
within 250 ms of the Mac clock. Synchronize phone/Mac wall clocks for the demo.
Receipt is not progress: only a pose or bundle with a strictly newer `t_capture` than any
accepted so far renews freshness, so repeats with fresh wall times go stale. Pose and
bundle streams are ordered independently, so a bundle delayed behind newer poses still
maps with its own transform (`/capture/status` `mapping` counts `discarded_order`,
`discarded_wall_time`, `discarded_tracking`) but never renews or rewinds the pose. Frames
captured at or before a limited/unavailable capture are not mapped after recovery, and
recovery never re-arms. Progress state is per phone connection.
Older capture timestamps are discarded. Tracking loss, stale pose, phone loss,
map reset, mode switch and operator stop disarm and log zero drive. They never
send hardware commands. `/session` revokes the old phone connection; it must reconnect.

## Camera and full sensor capture

Open `http://<mac>:8765/capture` while the iPhone streams to `/phone`. The page shows native camera, raw/smoothed depth, confidence, optional people masks/depth, telemetry, calibration, geometry counts, and recording state. Missing data stays unavailable. The older phone app still supplies standard RGB and raw depth/confidence previews. **Pause preview** freezes the browser image; **Save frame** saves the displayed JPEG/PNG.

Full packets arrive through a separate `POST /capture/ingest`. By default their exact bytes are recorded in gitignored `backend/captures/<session-hash>/`, capped at 8 GiB per session with a 1 GiB free-space reserve. Set `GODSEYE_CAPTURE_DIR` to another directory, or an empty string to disable recording. Disk errors/limits are shown in the app and page; capture continues. Export/delete old sessions manually. No image recording is added for v1-only senders.

See [docs/CAPTURE.md](../docs/CAPTURE.md) for the v2 envelope, full sensor inventory, timestamps, units, download/preview routes and limitations. Live buffers are bounded and cleared on disconnect/reset; disk recordings persist. Full capture does not refresh drive health or change the frozen v1 interface. These development routes use the same trusted private network as the rest of the backend.

## Live map points

Dense-capable viewers may negotiate `godseye.points.v2` on `/live`: a little-endian
uint32 JSON-header length (header padded to a multiple of four), header fields
`version:2`, `type:points`, `chunk_id`, `session_id`, `map_epoch`, `frame_id`,
`t_capture`, `count`, `positions:float32_le`, `colors:rgb8_srgb`, followed by XYZ
float32 meters and RGB uint8 sRGB. Frames contain at most 20,000 points. While a
dense viewer is connected, mapping targets 30 Hz and samples at least 20,000
candidate depths; actual output still passes main's voxel-memory/occupancy gates.
Legacy viewers receive JSON subsets capped at 2,500 points and 4 Hz. With only
legacy viewers, existing point settings and cadence remain unchanged. Per-viewer
queues keep two pending point packets and never block phone intake. Capture rate
and actual Wi-Fi/processing throughput can be lower than these targets.


Each valid `/phone` frame bundle can become one `/live` `points` message
(`backend/mapping.py`, pure and hardware-free). The newest bundle replaces any
older one still waiting, and at most one computation runs at a time in a worker
thread, so the WebSocket loop never blocks and memory stays at one pending plus
one in-flight bundle per phone.

```json
{ "version": 1, "type": "points", "chunk_id": 24, "session_id": "uuid", "map_epoch": 1,
  "frame_id": 171, "t_capture": 5.70,
  "positions": [x, y, z, ...], "colors": [r, g, b, ...] }
```

- `positions`: flat ARKit world meters (+Y up, right-handed), rounded to 1 mm.
  `colors`: flat floats in 0..1 (same length, same order; matches `tools/fake_live.py`).
- Up to **2500 points** (about 90 KB of JSON) per chunk, at most **4 chunks/s**.
  `chunk_id` counts from 1 within a session/epoch and continues across phone reconnects to the same map while the backend is running.
  Chunks are **not cumulative**: the
  dashboard appends and caps its own total, and should clear its cloud when
  `session_id` or `map_epoch` changes.
- Points are depth pixels with confidence 2 (high), finite depth from 0.05 to
  5 m, evenly subsampled. Their JPEG-scaled intrinsics and the bundle's own
  camera-to-world transform place them, with the same geometry as
  `localization.py`. Frames with fewer than 16 usable pixels, bad bundles, and
  non-`normal` tracking produce no chunk; the phone link stays up.
- **Chunks carry mostly newly observed voxels** (`backend/point_dedupe.py`).
  Each frame back-projects up to `GODSEYE_POINT_SAMPLES` candidates, keeps one
  per world voxel of `GODSEYE_POINT_VOXEL_M`, drops voxels already sent in this
  map, and publishes up to `GODSEYE_POINTS_PER_CHUNK` of the rest, evenly spread
  over the image. A frame with nothing new publishes no chunk (`map_stats`
  counts it as `no_new_points`); a chunk dropped as stale is not remembered.
  A voxel may be sent again once it is `GODSEYE_POINT_REFRESH_S` old by
  capture time, so a moved object or drifted surface is not frozen forever.
  The memory is shared by all viewers and resets only when the session/epoch
  changes (including `/session`); a phone reconnect to the same map keeps it.
  A dashboard opened mid-session fills in as the camera sees new surfaces and
  as revisited ones pass the refresh age. Occupancy still gets every candidate.
  `tools/fake_phone.py` moves its depth ramp with the camera, so its world
  surface never repeats and it keeps producing full chunks.

  | Variable | Default | Range / meaning |
  |---|---|---|
  | `GODSEYE_POINT_VOXEL_M` | `0.02` | 0–1 m; `0` disables dedupe (old behavior) |
  | `GODSEYE_POINT_SAMPLES` | `10000` | 1–49152 candidates per frame (one full 256×192 depth map) |
  | `GODSEYE_POINTS_PER_CHUNK` | `2500` | 1–2500; 2500 is a hard max (dashboards reject more) |
  | `GODSEYE_POINT_REFRESH_S` | `60` | seconds before a voxel may be resent; `0` never resends |
  | `GODSEYE_POINT_MAX_VOXELS` | `2000000` | 1–20,000,000 remembered voxels |

  Invalid values stop startup with an error naming the variable. Memory costs
  16 bytes per remembered voxel (~32 MB at the default cap, briefly doubled
  while merging); past the cap the oldest-sent voxels are evicted down to 90%.
  Dedupe and merging run in the worker thread (about 5 ms per frame at a full
  2M-voxel memory on an M5 Pro); the event loop only queues the sent keys.
- A chunk is dropped if the session/epoch changed or the phone left while it was
  computing, or if its frame is more than 1 s old when finished.
- A viewer that stops reading keeps at most 2 pending `points` messages; they
  never displace health or pose.
- Depth is optical-axis meters. Nothing here is object detection, occupancy, or
  navigation, and the synthetic phone's gradient is only a transport check.

Live check without a phone, car, or dashboard (each in its own terminal):

```sh
$HOME/.venvs/godseye/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8765 --ws-max-size 8388608
$HOME/.venvs/godseye/bin/python tools/fake_phone.py --url ws://127.0.0.1:8765/phone --frame-hz 10
$HOME/.venvs/godseye/bin/python tools/probe_live.py --url ws://127.0.0.1:8765/live
```

Hand-off checks. **iOS:** send a bundle per the `docs/INTERFACES.md` layout
(hello's `session_id`/`map_epoch`, wall time within 250 ms of the Mac,
`tracking: normal`); the probe must then show `points > 0`. Physically check
that a wall in front of the phone appears in front of the pose in ARKit world
coordinates and does not orbit when the phone turns. **Dashboard:** subscribe
to `ws://<mac>:8765/live`, append `points` chunks (colors 0..1), cap the total,
and reset on a new `session_id`. Neither real-device nor dashboard paths were
exercised in this slice; the shared checkpoint passes only when both are.

## Occupancy

The same back-projected candidates (up to `GODSEYE_POINT_SAMPLES`, default
10000, per mapped frame, before voxel dedupe) also feed a 2D grid
(`backend/occupancy.py`, pure and hardware-free). The mapping worker thread only
voxelizes them; the event loop commits that evidence under the same checks as the
frame's points chunk, including frames whose chunk had no new points. A separate
task snapshots the grid in a worker thread at most once per second and publishes
only when the picture changed:

```json
{ "version": 1, "type": "occupancy", "session_id": "uuid", "map_epoch": 1,
  "origin": [-4.5, -5.85], "cell_m": 0.05, "width": 177, "height": 109,
  "cells": "base64 uint8", "floor_y": -1.2 }
```

- `cells` is `width * height` bytes, row-major: byte `row * width + col` is the
  cell whose min corner is `origin + [col, row] * cell_m` in world (x, z), so
  rows run along +z. 0 = unknown, 1 = free, 2 = occupied. The grid is the
  bounding box of known cells. `session_id`, `map_epoch` and `floor_y` (world Y
  meters of the estimated floor) are additive to the frozen v1 shape.
- **Floor assumption:** the phone's height is unknown and the AR origin is where
  the session started, so the floor is estimated from the evidence: the lowest
  2 cm height slice covering at least 25 distinct cells and at least twice the
  cells of the slices 6-16 cm above and below it (a wall covers every slice
  equally). Until a floor is found nothing is published. A table top or ceiling
  can only be mistaken for it when no floor has been seen.
- **Thresholds:** evidence is counted once per frame per 5 x 5 x 2 cm voxel. A cell
  is free with at least 2 hits within 4 cm of the floor, and occupied (winning
  over free) with at least 3 hits from 8 cm to 1.5 m above it that also reach
  10% of its free hits. The 4-8 cm gap, anything below the floor, and anything
  above 1.5 m (ceiling, overhangs) are ignored.
- **Caps:** x and z within 10 m of the AR origin (at most 400 x 400 cells, about
  213 KB of base64) and Y within 4 m; other points are dropped. At most 500,000
  voxels (about 6 MB) per map; new voxels beyond that are dropped.
- The grid belongs to one session/epoch: a map reset starts an empty one, a
  phone rejoining the same map keeps it, and a grid finished after the session or
  phone changed is counted and dropped. A frame still mapping when its phone left
  or its map reset, or finished more than 1 s after it arrived, adds nothing to
  any grid, so a resumed map holds only accepted frames. New `/live` viewers and
  map resets get the current grid, if any, after the objects and path snapshot. A
  slow viewer holds only the newest pending grid.
- `OccupancyGrid.revision` bumps whenever an accepted frame adds evidence
  (navigation's blocked-path check keys on it). `accepted_at` is the event loop's
  monotonic time of the newest accepted frame, even one that adds nothing, so a
  planner can tell fresh sensing from a stale map without waiting for a new
  `occupancy` message.
- **Limitations:** without a motion-ready rover calibration (below) the 8 cm to
  1.5 m band is a generic guess, not its clearance; the handheld floor estimate
  can shift by a slice as evidence grows; there is no free-space ray carving (cells are only
  known where a surface was seen) and no decay, so a removed object stays
  occupied. `tools/fake_phone.py`'s gradient depth has no horizontal plane, so it
  produces no grid; tested on synthetic floors and boxes only
  (`backend/tests/test_occupancy.py`).

## Rover calibration and the navigation map

Occupancy is safe to plan motion on only once the rover's own geometry is
measured. `GODSEYE_ROVER_CALIBRATION` names a JSON file validated at startup by
`backend/calibration.py` (a malformed or missing file stops the server; unset
means uncalibrated). There are no defaults: until Tomiwa's measurements exist,
the file holds nulls and the map stays **not motion-ready**:

```json
{ "version": 1, "measured_by": null, "obstacle_min_m": null,
  "footprint_length_m": null, "footprint_width_m": null, "clearance_margin_m": null,
  "camera_forward_m": null, "camera_left_m": null, "camera_yaw_rad": null }
```

Pending hardware prerequisites, owned by Tomiwa: the lowest obstacle height the
car cannot drive over; chassis length and width including bumpers; the clearance
margin to keep; the phone camera's offset from the chassis center (forward,
left) and its yaw relative to the chassis; and `measured_by`, naming who measured
and where the evidence lives. Steering/speed response, stopping distance and
bench-stop evidence are separate, and no backend test stands in for any of them. Meters and radians;
bounds only reject typos (a footprint side over 1 m, centimeters, NaN, unknown
keys).

- A complete, verified calibration replaces the generic 8 cm obstacle floor with
  `obstacle_min_m - 0.02` for both `/live` and the snapshot: a voxel's height
  can read one 2 cm slice low. A threshold at or below 6 cm is
  `obstacle_min_unsupported`, since those hazards read as floor noise within
  4 cm of the floor. Such a car needs better sensing, not a smaller number.
- The footprint is covered by a disc around the camera's floor point (the v1
  rover position): radius `hypot(length / 2 + |forward|, width / 2 + |left|)`
  plus the margin, so no heading or rover base frame is needed for inflation.
  Navigation subtracts `camera_yaw_rad` from camera heading to steer by chassis
  heading; the live camera pose and v1 position coordinates remain unchanged.

`app.state.map_snapshot()` is the navigation map handle: it classifies the active
map's evidence (blocking; call it from a worker thread) and returns None without
an active map. Otherwise it returns an immutable `OccupancySnapshot` for the
current session/epoch only: `cells` in the `/live` layout with `origin`,
`floor_y`, `blockers` (empty exactly when `ready`), `inflation_m`, and the grid's
own `revision` and `accepted_at` (above), read together with the evidence. There
is no separate counter: repeated views keep `accepted_at` fresh even when `/live`
sends no new grid, and dropped frames never refresh it. The staleness limit is the
navigation owner's policy. `traversable(x, z)` is False unless the map is ready
and every cell with any part within `inflation_m` of the point is known free;
unknown, occupied, off-grid and nonfinite queries block. A map reset starts an
empty snapshot (revision 0, no `accepted_at`) and keeps the calibration.
Calibration never arms or drives.
Tested on synthetic floors and boxes only (`backend/tests/test_calibration.py`),
with TEST values that describe no real car.

Navigation consumes this handle in worker threads. Both goals and exploration refuse
its blockers, absent sensing, and sensing older than 1 s (`NavSettings.map_max_age_s`).
Pose freshness cannot substitute for accepted depth evidence. See Navigation below.

## Live objects

With a detector configured, each valid `/phone` bundle can also become object
observations (`backend/objects.py`). Detection has its own newest-wins mailbox and
runs at most **2 inferences/s**, one at a time, in a worker thread. Model calls
are serialized across phone reconnects, and work queued behind a slow inference
is skipped once its phone has gone. Results are applied on the event
loop only if the phone and session/epoch are unchanged and the frame arrived at
most 2 s earlier; otherwise they are counted and dropped.

- Every localized detection is stored as its own `observations` row: box, raw
  world position, depth/sample count, detector confidence, frame and object ID.
- It is then folded into one `objects` record for that session/epoch. The nearest
  same-class object within **0.5 m** keeps its UUID; `position` and `confidence`
  become running means over its observations, `observations` counts them, and
  `first_seen`/`last_seen` are phone wall-clock seconds. Otherwise a new object
  starts. Each object takes at most one detection per frame, so two same-class
  items seen together never merge. A sighting marks its object `present`
  (`moved` is kept); rescans also use `last_seen`, `moved` and
  `not_found_on_rescan` (see Rescan and change events).
- `confidence` is the mean detector score, not a calibrated identity probability.
  `position` is the depth surface point under the box center (`DETECTION.md`).
- Objects never cross sessions or epochs. A phone that reconnects with the same
  `session_id`/`map_epoch` resumes that map and its IDs, including after a restart.

`/live` sends the full snapshot after any change (on change, not per frame), up
to 256 objects, newest `last_seen` first:

```json
{ "version": 1, "type": "objects", "session_id": "uuid", "map_epoch": 1,
  "objects": [{ "id": "uuid", "class": "cup", "position": [x, y, z], "confidence": 0.8,
                "first_seen": 1790380848.1, "last_seen": 1790380851.6,
                "observations": 7, "state": "present" }] }
```

`GET /objects` returns the same body without `type`: the active session, or the
most recently created one when no session is active (so it survives a restart).
`session_id`/`map_epoch` are additive to the frozen v1 shape; they are null
before any session exists.

Detection is off unless `GODSEYE_YOLO_WEIGHTS` names existing local weights
(`create_app(weights=...)`, or `detector=` with any object that has
`localize(frame)`, or `detect(frame)` plus `confidence` to also report boxes
that lack depth). Missing weights fail startup; nothing is downloaded, and
`YOLO_OFFLINE=1` is set so Ultralytics skips its online checks. Health
`detector` is `down` without a detector, `ok` within 2 s of a used result, else
`stale`; map reset and phone loss clear it. `/arm` still refuses because the car is down.

Local smoke with existing weights (a photo with fixed synthetic depth; no phone):

```sh
GODSEYE_YOLO_WEIGHTS=$HOME/.venvs/godseye/yolo11n.pt $HOME/.venvs/godseye/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8765 --ws-max-size 8388608
$HOME/.venvs/godseye/bin/python tools/fake_phone.py --url ws://127.0.0.1:8765/phone --image "$($HOME/.venvs/godseye/bin/python -c 'import ultralytics, pathlib; print(pathlib.Path(ultralytics.__file__).parent / "assets/bus.jpg")')"
$HOME/.venvs/godseye/bin/python tools/probe_live.py --url ws://127.0.0.1:8765/live
curl -s http://127.0.0.1:8765/objects
```

The probe should list `bus` and `person` objects with growing `observations`
and health `detector: ok`. The fake phone turns while the photo stays fixed,
so the same person drifts in world space and can split into several IDs; this
checks the pipeline, not recognition or localization accuracy. Neither a real
phone scene nor the dashboard has been exercised against these objects yet.
Fake-detector behavior tests (no GPU or weights):
`$HOME/.venvs/godseye/bin/python -m unittest backend.tests.test_objects -v`.

### Live detection overlay

Each accepted detection result (same 2 Hz worker, same reset/disconnect/age
checks as objects) also publishes one additive `detections` message, even when
the frame contains nothing, so the dashboard can show what the detector saw:

```json
{ "version": 1, "type": "detections", "session_id": "uuid", "map_epoch": 1,
  "frame_id": 812, "t_capture": 1234.56, "t_wall_ms": 1790380851600,
  "image": { "width": 1280, "height": 960 }, "source": "backend_detector",
  "classes": ["person", "backpack", "chair", "bottle"],
  "detections": [{ "class": "person", "confidence": 0.87, "box": [x1, y1, x2, y2],
                   "position": [x, y, z], "depth_m": 2.1, "object_id": "uuid" }] }
```

- `box` is xyxy in that frame's own JPEG pixels. Every 2D box is listed, highest
  confidence first (at most 32), whether or not it could be placed.
- `position`/`depth_m`/`object_id` are non-null only when the same capture's depth
  localized the box (the rules above) and it was stored as an observation;
  otherwise the box stays 2D evidence and nothing is placed in 3D.
- Only `classes` are listed: `GODSEYE_OVERLAY_CLASSES` (comma separated), default
  the staged demo set `person, backpack, chair, bottle`, reduced at startup to
  the classes the loaded weights can emit (all four are COCO/YOLO11 classes).
  Object memory is unchanged and still stores every localized class.
- `GET /capture/detections.jpg?session_id=&map_epoch=&frame_id=` returns that
  frame's exact JPEG only while it is the newest accepted detection frame of the
  active map; any other frame or map, or after a map reset or phone loss, is
  `409` (never a substitute image). The backend keeps just this one JPEG in memory.

Tests (no weights): `$HOME/.venvs/godseye/bin/python -m unittest backend.tests.test_detections -v`.

### Suggested approach route (visualization only)

`POST /route` `{ "session_id", "map_epoch", "object_id", "start": [x, z] }` returns
a suggested walking route from an operator-selected entrance/start to a
remembered `person` in the active map (`backend/approach.py`). It reuses the
navigation grid A* on the current occupancy classification, with these demo
clearance assumptions instead of the car footprint: a 0.5 m wide walker
(0.25 m radius) plus 0.05 m margin, observed-free cells only (unknown space is
blocked, never filled in; doors are not inferred), and a 0.3 m keep-out around
the person. The route ends at the nearest observed-free point within 1.5 m of the
person, never on the person's cells. The response echoes the map, object, start and
occupancy revision. It has `status: "ok"` with `points`, `approach` and `length_m`,
or `status: "unavailable"` with a `reason` (`no_observed_map`,
`start_not_observed_free`, `no_observed_free_approach`, `no_observed_free_route`,
`start_or_person_off_map`), always with `assumptions` and `verified: false`.
A wrong map returns `409` and an unknown or non-person object returns `404`. It never sets a goal,
publishes `path`, arms or commands motion. The dashboard re-requests it on new
occupancy or a moved person and drops it on a map reset.

`app.state.approach_view` is the current selected route for read-only consumers
(voice answers): the newest `/route` response dict plus `t_wall_ms`, or `None`.
An unavailable result replaces an earlier success. A `404` selection, a map reset,
a newly published occupancy picture that differs from the cells it was planned on,
and a sighting that moves, merges or removes the person all set it to `None`.
A slower, older request never overwrites a newer selection. Tests:
`$HOME/.venvs/godseye/bin/python -m unittest backend.tests.test_approach -v`.

## Rescan and change events

`POST /rescan` (`backend/changes.py`) needs an active map with at least one
stored frame; an empty baseline reports every confirmed object as `new`. For
each object (up to 256, most recently seen) it freezes the newest raw
observations (up to 20) as a baseline cluster: the median of the ones agreeing
within 0.3 m with the newest three, with their RMS spread. Only frames captured
after the newest stored frame at the press are revisit evidence (late inference
for earlier frames never counts); they form separate revisit clusters the same
way. Neither side is the running mean in `objects`. Every object becomes
`last_seen` until a post-press sighting makes it `present` again. A new press
starts a new baseline; the latest one resumes after a restart or phone reconnect
of the same session/epoch. Press it before moving a prop (or keep the new spot
out of view until then): anything seen earlier is baseline.

```json
{ "version": 1, "session_id": "uuid", "map_epoch": 1, "rescan_id": "uuid", "baseline_objects": 3 }
```

Each detection frame also depth-probes the remembered spots not yet seen again
(`localization.view_status`): `clear` when high-confidence depth passes more than
0.15 m beyond the spot (a remembered surface point cannot be seen through),
`surface` when it ends there, else `occluded` or `out_of_view`. A spot is empty
after 3 `clear` frames and no `surface` frame.

A revisit cluster is confirmed by 3 agreeing observations (one per frame).
Alignment is `ok` when at least 2 confirmed static objects are reobserved within
0.15 m of their baseline (no looser than the probe margin), and `drifted` when
their median shift exceeds that or two candidate moves share a displacement;
drift suspends every movement claim.

- **`new`**: a confirmed cluster of a class with no unseen remembered object.
- **`possible_move`**: a confirmed new-identity cluster at least
  `max(0.6 m, 3 × (both spreads))` from the nearest unseen same-class memory,
  while identity is unproven. The two stay separate objects.
- **`moved`**: as above, with verified alignment, exactly one unseen memory and
  exactly one new identity of that class sighted since the press (even a single
  unconfirmed glimpse of another competes), a confirmed baseline, and its old
  spot empty.
  The new identity is folded into the remembered one (its observations are
  relinked, measurements untouched), which moves there with state `moved`.
- **`not_found_on_rescan`** (state only): an unseen memory whose spot is empty
  with verified alignment; a later `surface` reverts it to `last_seen`. No
  `not_found` events are emitted, and nothing is ever called removed.

Out-of-view objects simply stay `last_seen`. Nearer same-class sightings (within
0.5 m) keep the remembered ID, and ones under the displacement threshold report
nothing. Each (kind, object) is recorded once per rescan, so `possible_move` can
be followed by `moved`.
`/live` sends each event once, after the objects snapshot it changed:

```json
{ "version": 1, "type": "event", "session_id": "uuid", "map_epoch": 1,
  "id": "uuid", "rescan_id": "uuid", "kind": "moved", "object_id": "uuid",
  "new_object_id": "uuid", "old_position": [x, y, z], "new_position": [x, y, z],
  "displacement_m": 1.5, "t": 1790380851.6 }
```

`object_id` is the remembered object for move kinds and the new one for `new`,
whose `old_position` and `displacement_m` are null. `id`, `rescan_id`,
`new_object_id` (the new sighting's object; after a `moved` merge, the surviving
ID), `session_id` and `map_epoch` are additive to the frozen v1 shape. `t` is the
confirming frame's phone wall-clock seconds. `GET /events` returns
`{ version, session_id, map_epoch, events }` for the shown map (up to 256, oldest
first) without the per-message `version`, `type`, `session_id` and `map_epoch`.

`events.confidence` stays empty: there is no calibrated identity confidence yet.
Evidence counters for empty spots live in memory and restart from zero after a
backend restart (clusters and events come back from SQLite). Identity rests on
class uniqueness plus the empty old spot, which suits one distinctive prop but
is not general re-identification. Thresholds are initial values (0.6 m and three
observations come from the spec), tested only on synthetic scenes
(`backend/tests/test_changes.py`); real-scene accuracy, depth noise and ARKit
drift are unmeasured.

## Navigation

`POST /goal` and explore mode plan on the active session's occupancy grid
(`backend/navigation.py`, pure) and follow the path in one asyncio run at a time
(`backend/navigator.py`). Each follower command goes through
`Motion.submit` under the arm generation the run started in, so it reaches the
car only via the leased 20 Hz pump (see Drive commands). `/arm` still refuses
while the default car reports down, so runs are exercised by tests with
`FakeCar`.

- **Starting:** `/goal {x, z}` needs the backend armed in `navigate` mode (409
  otherwise; there is no disarmed preview). It plans from the current pose (at
  most 250 ms old), answers `{version, goal, points}`, publishes `path` and starts
  following; a new goal replaces the current run. The arm generation is read
  before planning, so a stop and re-arm while it plans answers 409 `Stopped
  while planning` and the old goal never moves. Explore starts by itself
  whenever the backend is armed in `explore` mode and drives to the nearest
  reachable frontier (a known-free cell next to unknown or the edge of the cropped
  grid), then the next, until none is left. Before a calibrated floor is mapped, both goals and explore stop with the
  map readiness reason; neither can move into unknown space.
- **Planning:** 8-connected A* on the 5 cm cells, no corner cutting, line-of-sight
  shortcuts, waypoints at most 0.25 m apart, at most 200,000 expansions. Every path
  cell must be known free throughout the calibrated `inflation_m` footprint disc.
  Clearance counts entire occupied/unknown cell squares and off-grid space, not
  just cell centers; this deliberately rejects some tight passages that fit at a
  single point. No start/goal snapping or unknown padding can escape a blocker.
  The pure planner's legacy simulation defaults are overridden at the snapshot seam.
  Explore selects the nearest reachable boundary of footprint-clear known floor,
  inset from unknown space so its footprint stays observed; it completes when none remains.
- **Following:** pure pursuit (0.35 m lookahead) at 10 Hz, one `submit` per
  tick, cruising at 0.15 m/s, slowing within 0.40 m of the goal and clamped to
  the contract's 0.20 m/s and 0.5 rad/s (then to the motion limits). Heading errors above 0.6 rad turn in
  place (`v_mps` 0). It never reverses; arrival is within 0.15 m.
- **Replanning:** a full replan from the current pose about once per second; snapshot
  checks run at most 4 Hz regardless of `/live` publication. Each reads accepted
  sensing time, and a new revision checks the remaining path. An obstructed path
  ends the run with `path_blocked`, zeroes and disarms. Current and next-tick pursuit
  footprint positions must also remain clear, so pursuit cannot cut an unsafe corner.
  Reaching an explore frontier discards pending planning/check work before selecting
  the next frontier (the landed exploration race fix). `path` is published only when
  its points change, and new `/live` viewers get the current path.
- **Stops:** every run ends through the same `stop(reason)` as `/stop` (disarm,
  zero drive, health event) and publishes an empty `path`; health reports the
  reason. Map blockers (`calibration_missing`, `calibration_unverified`, unmeasured
  fields, `obstacle_min_unsupported`, `no_floor`), `map_unknown`, `sensing_stale`,
  `path_blocked`; `arrived`, `explore_complete`; `no_path`, `search_limit`,
  `destination_blocked` (goal occupied or inside the inflation), `destination_unknown`,
  `out_of_bounds`, `start_blocked`; `pose_stale` (no pose within 250 ms),
  `tracking_lost`; `no_progress` (motion commanded while the pose moved under
  5 cm and 0.15 rad for 5 s); `nav_error` (planner or loop failure); `disarmed`
  (defensive: the armed mode changed without a stop); `command_stale` (Motion
  refused a command because its generation ended). `/arm` also ends any run. Operator stop, mode change,
  map reset, phone loss, tracking loss, the pose watchdog and shutdown end the
  run through `stop` as well. A `/goal` that cannot be planned answers 409 with
  the reason and disarms.
- **Unverified interface dependencies** (rover issue #6): positive
  `yaw_rate_rps` means increasing `yaw_rad`, a left (counterclockwise from above)
  turn with +Y up, and the car adapter must confirm that sign; heading is the
  camera forward corrected by the measured camera-to-chassis yaw; turning in place assumes a skid- or differential-steer
  base. Radius and margin now come from the explicit calibration; speeds and
  follower tolerances remain software limits, not measured car-response parameters. With a car that never moves (`FakeCar`), a live run ends with
  `no_progress` after 5 s.
- **Tests:** `backend/tests/test_navigation.py` (planner, follower, 400 x 400
  timing) and `backend/tests/test_navigator.py` (runs against a kinematic
  stand-in rover, `/goal` and app-level stops).

## REST

All successful responses carry `version: 1`. Errors use FastAPI's standard
`detail` envelope. Request bodies follow the frozen contract (no version field required).

| Route | Skeleton behavior |
|---|---|
| POST `/session` | Create map identity, revoke phone, clear current pose and disarm |
| POST `/arm` | 409 while any health component is not ok (the default logging car always reports down); opens a fresh command generation |
| POST `/stop` | Always accepted; latch operator stop, end any navigation run and send an explicit zero |
| POST `/mode` | Stop first, then select manual/navigate/explore |
| POST `/manual` | Validate finite bounds (±0.20 m/s, ±0.5 rad/s); 409 when disarmed or not in manual mode; otherwise hold the command for a 250 ms lease and return health |
| POST `/goal` | Validate x/z; 409 unless armed in navigate mode; plan and follow (see Navigation) |
| POST `/rescan` | Freeze a baseline of the active map and start the revisit; 409 without a map or any stored frame |
| POST `/ask` | Search saved class/identity facts for the shown map; return grounded matches and positions |
| GET `/voice`, POST `/voice/ask` | Push-to-talk Q&A: availability, then clip -> transcript -> grounded answer -> speech (see [VOICE.md](VOICE.md); off by default) |
| GET `/objects` | Versioned object snapshot (see Live objects) |
| GET `/events` | Versioned change events of the shown map (see Rescan and change events) |
| GET `/health` | Phone freshness, car adapter health, detector status, mode, armed, stop_reason |

`drive(v_mps, yaw_rate_rps)` in `drive.py` only logs; it contains no network,
serial, vendor, motor or credential integration. Startup is disarmed, and the
default `LoggingCar` adapter reports the car down, so the default backend cannot arm
or drive the rover. The opt-in iPhone/Bluetooth adapter is described in
[Autonomous rover integration](../docs/AUTONOMY.md); its measured profiles and explicit
arm/stop handshake are mandatory. Start it with `python3 -m tools.run_rover_backend`.

## Drive commands

`create_app(car=...)` takes a car adapter from `drive.py` with `send(command)`,
`zero(envelope)` and `health()` (`ok`/`stale`/`down`); the envelopes are
described under Command envelopes below. `LoggingCar` (default) logs through
`drive()` and reports down. `FakeCar` records calls (`('send', v, w)` or
`('zero',)`) and the full `envelopes`, and reports the health a test sets; it
moves nothing. A real adapter may report `ok` only from verified car feedback,
never from a successful write, must return promptly (its calls run on the
event loop). `rover_relay.py` implements the opt-in authenticated `/rover` phone
relay, with per-session acknowledgements, fresh ESP permits and matched Uno feedback.
`device_relay.py` provides separate `/device` mounted-phone setup controls, which
cannot arm or submit movement. `/autonomy` reports readiness blockers. Physical
autonomous movement remains unverified; see [the validation record](../docs/AUTONOMY.md).

`motion.py` holds at most one desired command per arm generation:

- `/arm` checks phone, car and detector health, zeroes, and opens a new
  generation. Every stop (operator, mode change, session reset, phone or pose
  loss, tracking loss, car/detector health loss, adapter error, shutdown)
  closes it, drops the held command and sends an explicit zero. A later arm
  never revives an older manual hold or goal.
- Only the watchdog's 20 Hz pump sends motion, at most once per tick, newest
  command wins. Before each send it rechecks arm, mode, phone freshness and
  tracking, car and detector health; any failure is a latched stop. While
  armed, the same health check also runs with no command held.
- Commands are clamped to 0.15 m/s and 0.5 rad/s (`MotionLimits`; speed may
  be configured up to the 0.20 m/s contract maximum, never above). A command
  not renewed within 250 ms is zeroed once; the operator stays armed.
  Non-finite input zeroes and raises.
- An adapter `send` error stops with `car_error`. A failed `zero` also stops
  with `car_error`, is retried every tick with nothing else sent, and blocks
  `/arm` until the car accepts one.
- `/manual` carries no token in the frozen v1 wire, so a delayed request or a
  second dashboard still holding the button counts as fresh input after a
  re-arm. Only one operator surface should drive at a time.

Navigation uses the same boundary: `/goal` reads `app.state.motion.generation`
before planning, and every 10 Hz follower step calls
`app.state.motion.submit(generation, mode, v_mps, yaw_rate_rps)`. `False` means
the generation ended, so the run stops (`command_stale`) and publishes an empty
path. Navigation owns goal and path policy; this boundary owns dispatch safety.

### Command envelopes

The adapter receives `DriveCommand` and `DriveStop` from `motion.py`. They are
transport-neutral. The bridge endpoint, wire encoding, UART framing,
acknowledgement and health semantics, PWM speed calibration and phone mount
transform are not chosen yet. Field names follow the phase-2 draft in
`docs/INTERFACES.md` section 4, which stays unchanged. `map_epoch`, `mode`,
`arm_token` and `version` are left out; the per-arm `session_id` does the
arm token's job.

- `session_id`: a drive session, not the phone map's `session_id`. Each
  successful `/arm` opens a fresh one (uuid4 hex). The next stop ends it with a
  `DriveStop`, and no new session opens until that Stop was handed off. A Stop
  before the first arm carries `None`.
- `seq`: starts at 1 in each session and rises strictly across its commands
  and Stops. A failed hand-off may leave a gap.
- `issued_at_ms`: the backend's `time.monotonic()` in ms when it accepted the
  velocity (the latest `/manual` or follower submit), or when it made a zero or
  Stop. Resends of a held command repeat it. Its origin is arbitrary, so it means nothing to phone
  (`t_capture`), wall, bridge or UNO clocks. `valid_for_ms` is the 250 ms lease;
  together they state the backend's lease, rounded to the millisecond. The
  backend's expiry uses only its own monotonic clock.
- A lease expiry or refused input while armed hands off a zero `DriveCommand`
  in the open session. The operator stays armed, and a renewed hold continues
  that session. Every stop hands off a `DriveStop` through `zero()`. A Stop that
  fails is retried unchanged every tick until the car accepts it or a later stop
  replaces it with a newer one, and it blocks `/arm` meanwhile.
- Nothing is handed off while armed and idle, between a zero and the next held
  command.

What firmware must do, independently of this backend and unproven here: honor
every Stop, and never resume a session that a Stop, a lost link or a newer
session ended. Only a new session may move the car, adopted only at `seq` 1:
the backend mints one only on `/arm`, and its first command is always seq 1.
A car that restarts mid-session therefore waits for a rearm, and a lost first
command leaves it stopped until then. It must drop any `seq` at or below the
last one in the session and stop the motors within 300 ms of the last valid
command, by its own clock. It must never act on a command more than 500 ms
after issue.

Still open:

- With unsynchronized clocks the car cannot age a command from `issued_at_ms`
  alone. How the 500 ms limit is measured (bridge receipt time, a clock-offset
  handshake, or another method) is undecided.
- Session ids are uuid4 and carry no order. A car with bounded memory can
  recognize only the sessions it remembers, so a replay of an older one falls
  to the 500 ms limit, unless the wire adds an ordered arm counter.
- Whether the 300 ms command-loss stop latches. If it does, any idle pause
  longer than 300 ms needs a re-arm, or the backend must add idle keepalive
  zeros, which it does not send today.

Proof is `backend/tests/test_motion.py` (fake clock),
`backend/tests/test_command_envelope.py` (envelope sessions, seq, issue time,
expiry, Stop retry and link loss; its `Receiver` models the proposed car rules
only to show the fields suffice), `backend/tests/test_drive_safety.py` (real app,
streaming phone, fake detector and `FakeCar`) and the navigation cases in
`backend/tests/test_navigator.py`.
This is backend evidence only. The Mac's lease is not the car's independent
300 ms stop or 500 ms limit, and no test here validates a physical protocol,
stop distance or bench behavior; those need hardware evidence and explicit
approval before any physical adapter is connected.

## Gemini crop labels and saved-object search

`backend/labels.py` owns the injectable async `LabelProvider.identify(jpeg,
class_name)` seam. `create_app(label_provider=fake)` enables offline acceptance
checks. No provider is installed by default. A newly localized object gets one
tight, edge-clipped JPEG crop (no surrounding padding), resized to at most
256 pixels per side. At most the first 32 detections in a frame get crops.
Crops exist only in memory; they are never added to the capture archive.
Labels do not change detector classes, association, movement evidence or
confidence scores, and are model descriptions rather than verified identities.

The SQLite `object_labels` table caches the result per stable object ID across
repeat sightings and restarts. One async worker has at most one call in flight
and 16 queued crops, with a six-second deadline (the HTTP adapter also has a
five-second network timeout). A persisted budget allows at most 64 queued
attempts per session/epoch, including failures and objects later merged away.
Overflow, budget exhaustion and missing configuration do not retry. A pending
attempt interrupted by restart becomes `error` with reason `interrupted`.

`GET /objects` and live object snapshots add an `identity` field without
changing the existing v1 facts:

```json
{"label":"red ceramic coffee mug","status":"labeled","reason":null,"source":"gemini"}
```

Statuses are `pending`, `labeled`, `unknown` (inconclusive), `unavailable`
(`disabled`, `invalid_crop`, `queue_full`, `limit`, or `not_requested` for older
objects), and `error` (`timeout`, `provider_error`, `interrupted`). Error text,
keys and raw provider responses are never stored or returned. Existing objects
are not automatically uploaded when the provider is enabled later.

`POST /ask` accepts the frozen `{ "question": "where's my red mug?" }` body.
It searches the complete persisted shown map (active map, otherwise newest),
including objects outside the 256-object live snapshot. It returns
`{ version, session_id, map_epoch, status, answer, matches }`. `status` is
`ok`, `no_match`, or `empty`; up to 20 newest matching object facts include
position in ARKit meters, state, phone-wall-clock `last_seen`, detector score
and identity status. Search is conservative lexical matching: all non-filler
question words must occur in the saved class/label, ignoring case and a trailing
plural `s`. It does not support semantic synonyms, arbitrary questions, scene
narration or claims that a last-seen object is still there. `/ask` questions are
never sent to Gemini (voice questions are; see [VOICE.md](VOICE.md)). Empty and unmatched queries report no saved evidence.

### Live provider opt-in (separate approval required)

Do not enable this until spend, image-sharing privacy, credentials and model
selection have separately been approved. The server process must receive
`GODSEYE_GEMINI_ENABLED=1`, `GEMINI_API_KEY`, and an explicit
`GODSEYE_GEMINI_MODEL` through local environment configuration. A key alone
does not enable calls; enabled configuration missing a key/model fails startup.
Do not commit keys or print them. The REST adapter sends only the selected crop
and detector class to Google's `generateContent` endpoint using the
`x-goog-api-key` header, not a credential in the URL. No phone permission,
recording, car arming or drive default changes are needed.

REST request/response reference: [Gemini generateContent](https://ai.google.dev/api/generate-content).
Offline verification:

```sh
$HOME/.venvs/godseye/bin/python -m unittest backend.tests.test_labels -v
```

These tests use synthetic phone imagery and fake label/HTTP responses only.
They do not establish live Gemini accuracy, paid API availability, physical
phone integration or dashboard rendering.

## Spoken change events

See [AUDIO.md](AUDIO.md) for offline-by-default synthesis, bounded audio playback,
restart-safe deduplication, deterministic tone demo, and the separate live-provider
spend/privacy/credential gate. No live ElevenLabs coverage is claimed.

### Explicit uncalibrated prototype

The operator may opt into `tools.run_rover_backend --prototype` with explicit
estimated chassis dimensions. It uses the phone manual-control default PWM with continuous commands; it does
not fill or certify measured calibration files.
Live map/floor, tracking, feedback and authentication gates remain in effect.
See [prototype setup and assumptions](../docs/AUTONOMY.md#uncalibrated-prototype-option).

Within an AR map, occupancy retains the last observed floor height when the
height histogram temporarily loses its floor peak. Current obstacle/free evidence
is reclassified at that height; sensing timestamps are not refreshed by this cache.
A new valid estimate can update it, and a new map epoch starts without a cached floor.
