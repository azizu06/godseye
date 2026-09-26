# Gods Eye Rover Preliminary Project Specification

**Event:** ShellHacks 2026  
**Version:** 0.1 — preliminary and extensible  
**Prepared:** September 25, 2026  
**Status:** Proposed design; hardware suitability and performance require testing

## 1 Executive decision

Build a smartphone-powered indoor rover that creates a live spatial map, places recognized objects in that map, and remembers their observation history. The intended distinguishing demonstration is a second scan that identifies a relocated object and displays its previous and current positions.

**Recommended purchase for the confirmed kit-plus-$20 budget: ELEGOO UNO R3 Smart Robot Car V4 with Camera, the first linked kit.** Its listing advertises a USB-chargeable lithium battery, while the linked ACEBOTT kit excludes batteries. That makes ELEGOO the more practical starting point when additional spending must cover the phone mount as well. Confirm that the exact seller package contains the advertised battery and charging provisions before purchasing. [ELEGOO listing](https://www.amazon.com/dp/B07KPZ8RSZ) · [ACEBOTT listing](https://www.amazon.com/dp/B0D8PV9TY9)

Use the included ESP32 camera board as a Wi-Fi-to-serial bridge, with the UNO retaining motor control and a local watchdog. The ESP32 camera feed is unnecessary; the iPhone supplies perception and the MacBook handles mapping and navigation. This adds a firmware boundary compared with ACEBOTT, but avoids buying another controller. ELEGOO documents reprogramming the ESP32 module. Confirm the delivered revision’s serial wiring and upload procedure before implementation. [ELEGOO camera-module programming guide](https://www.elegoo.com/blogs/learn/elegoo-smart-robot-car-v4-0-with-camera-upload-code-to-the-camera-module)

Start with slow forward motion and rotation on the conventional four-wheel base. Precise motion still requires calibration because skid steering can slip. Neither kit should be treated as an encoder-equipped precision robotics base until the delivered hardware is inspected.

The first deliverable is a dependable, supervised mapping rover. Full autonomous exploration is a later milestone. Keep the interface explicit about whether the rover is manual, navigating to an operator-selected destination, or exploring autonomously.

## 2 Project goals and planning assumptions

### Product purpose

God’s Eye gives a user a persistent, queryable account of an indoor space: what the rover observed, where it observed it, when it last saw it, and what appears to have changed. Potential future uses include lab inventory, room inspection, and locating misplaced equipment. Version 0.1 demonstrates these capabilities in a small controlled area; it does not establish production readiness.

### Preliminary assumptions

| Item | Working assumption |
|---|---|
| Competition | ShellHacks 2026; event rules, judging tracks, and actual build duration must be confirmed |
| Schedule | Illustrative 36-hour implementation window |
| Team | Four contributors, confirmed |
| Existing equipment | LiDAR-equipped iPhone 17 Pro and MacBook available |
| MacBook | Model and chip not yet selected by the team; benchmark before choosing detector size |
| Demo area | Approximately 3 × 4 m, flat hard floor, good lighting, no stairs or drop-offs |
| Objects | Three to five separated, recognizable props; one distinctive backpack for the change demonstration |
| Connectivity | Local network connecting phone, laptop, and ESP32; internet unnecessary for the core demo |
| Persistence | Within one continuous AR session, with saved logs for replay |
| Budget | One of the linked car kits plus at most $20 total for additions; use existing phone and laptop |

ShellHacks 2026 is the confirmed target. The official event interest page announces September 25–27; the precise hacking window, submission rules, and permitted preparation still need confirmation. The 36-hour schedule below is provisional. [Official ShellHacks announcement](https://interest.shellhacks.net/)

### Scope boundaries

Version 0.1 excludes outdoor operation, stair detection, public-space deployment, manipulation, facial identification, multi-robot coordination, and reliable cross-session localization. It also excludes a claim of complete room coverage: a low-mounted camera cannot observe every surface or object.

## 3 Hardware comparison and selection

The first link identifies the ELEGOO UNO R3 Smart Robot Car Kit V4 with Camera, ASIN B07KPZ8RSZ. The second identifies the ACEBOTT QD001, ASIN B0D8PV9TY9. Verify the selected variant and seller before purchasing. [ELEGOO listing](https://www.amazon.com/dp/B07KPZ8RSZ) · [ACEBOTT listing](https://www.amazon.com/dp/B0D8PV9TY9)

| Criterion | ELEGOO V4 with Camera | ACEBOTT QD001 |
|---|---|---|
| Control architecture | UNO R3 with a separate ESP32 camera module | ESP32 controller with car shield |
| Wireless integration | Available through the ESP32 side; custom commands may need a bridge to the UNO | Custom networking and motor logic can share the ESP32 |
| Drive arrangement | Conventional four-wheel chassis, operated by left/right drive | Four mecanum wheels; use forward/turn operation initially |
| Included perception | Camera and ultrasonic sensing; phone remains primary for this project | Ultrasonic sensing; optional camera expansion is unnecessary |
| Motor driver evidence | Listing specifies a dual-channel TB6612 driver | Manufacturer documents a motor-driving shield; exact driver and per-channel ratings require revision-specific verification |
| Encoders | Not verified as included | Not verified as included |
| Payload | No verified usable phone payload rating | No verified usable phone payload rating |
| Main project advantage | Conventional wheels and advertised rechargeable battery | Simpler controller/network architecture |
| Main integration concern | Additional firmware boundary and board revision differences | Wheel roller vibration, lateral drift, and motor calibration |
| Power package | Listing advertises USB-chargeable lithium battery; verify seller contents | Linked listing says batteries are excluded |
| Selection for current budget | Recommended, subject to package verification | Alternative if compatible power equipment is already owned |

Hardware references: [ELEGOO manufacturer product page](https://www.elegoo.com/products/elegoo-smart-robot-car-kit-v-4-0), [ELEGOO official software](https://github.com/elegooofficial/ELEGOO-Smart-Robot-Car-Kit-V4.0-New), [ACEBOTT kit documentation](https://acebott.com/docs/qd001-smart-car-starter-kit-for-esp32/), and [ACEBOTT car shield documentation](https://acebott.com/docs/qa052-esp32-car-shield-v1-0/).

**Why ELEGOO wins under this budget:** its conventional wheel arrangement suits a forward-and-turn rover, and the advertised rechargeable power system preserves the small accessory allowance for mounting the phone. The extra bridge firmware is a manageable tradeoff, provided both controllers can be flashed successfully. This is an engineering recommendation, not a measured performance comparison.

**ACEBOTT remains the simpler controller architecture.** Choose it if the team already owns compatible batteries and charging equipment and can still secure the phone within the $20 allowance. Its mecanum capability is unnecessary for the first demo. Do not buy additional camera, arm, encoder, or GPS expansion packs for this build.

### Purchase and acceptance checklist

- Verify exact controller/shield revision, matching example code, USB data cable, and supported programming workflow.
- Confirm the ELEGOO package includes the advertised rechargeable battery, charging connection, and required cable. If it does not, recompute the complete cost before purchase; the specification does not authorize spending beyond the $20 accessory cap.
- Confirm a rigid phone mount fits without covering the camera, LiDAR, ultrasonic sensor, or power switch.
- Test the actual phone, case, and mount as a combined payload. Do not assume the original concept’s desired 1 kg capacity is supported.
- Test slow starts, braking, repeated turns, tracking stability, and at least 15 minutes of powered operation with the payload.
- Verify local stop behavior before putting the phone aboard.
- Compare final delivered price and delivery date. This specification does not quote an unverified live price.

## 4 Required equipment and cost envelope

| Item | Quantity | Role | Procurement status |
|---|---:|---|---|
| ELEGOO V4 with Camera complete kit | 1 | Chassis, motors, UNO, ESP32 bridge, driver, obstacle sensor | Recommended |
| iPhone 17 Pro with protective case | 1 | RGB, depth, pose, optional mesh | Assumed existing |
| MacBook and charger | 1 | Development and runtime host | Assumed existing |
| Rigid phone mount and retaining strap | 1 | Fixed camera-to-rover geometry | Main use of accessory budget; reuse or fabricate if possible |
| Advertised kit battery and USB charging provision | 1 set | Motor and controller power | Verify included; reuse a compatible owned USB supply |
| Accessible kit power switch | 1 | Independent physical stop | Confirm it cuts motor power and remains reachable |
| USB data cable and adapter | 1 set | Firmware upload and bench debugging | Check package contents |
| Fasteners, wire retention, spacers, insulating materials | 1 set | Reliable assembly | Reuse kit/spare supplies; small capped allowance |
| Existing local network or owned access point | 1 | Local connectivity | No router purchase in baseline |
| Measuring tape and high-contrast props | 1 set | Calibration and acceptance tests | Required |
| Bumper switches or extra ranging sensors | Optional | Better blind-zone coverage | Outside current budget; future upgrade |

**Hard accessory cap: $20 total.** Allocate up to $12 for a rigid mount or mounting materials, up to $5 for retaining straps/fasteners, and keep $3 unspent for small missing parts. These are spending caps rather than verified product prices; include any accessory tax/shipping in the total. Prefer kit screws, borrowed tools, an existing phone case, an owned USB power supply, and an owned data cable. If no secure mounting solution fits, borrow or fabricate one before driving; do not exceed the cap by quietly adding components.

No extra ESP32, Arduino, motor driver, camera, LiDAR, power bank, or router is required by the baseline. The purchase formula is `selected kit checkout price + actual accessories`, with actual accessories no greater than $20. If the expected power equipment or necessary cable is absent, reassess the kit choice and accessory allocation.

Apple lists a LiDAR scanner on the iPhone 17 Pro. Runtime feature checks and a real-device test remain necessary before committing to the sensing pipeline. [Apple specifications](https://www.apple.com/ie/iphone-17-pro/specs/)

### Mechanical and power requirements

Mount the phone low and rigidly, with rear sensors facing the direction of travel. Begin approximately horizontal and adjust the tilt until the floor ahead and demonstration objects are both visible. Keep the sensor assembly fixed relative to the chassis; a loose vibration isolator can invalidate calibration.

Use the supplied motor shield and supported power arrangement first. Do not replace it with a generic driver merely because the concept mentions TB6612. Inspect driver ratings and regulation before adding loads; a shield’s overall supply rating does not establish safe motor stall current per channel. Keep the phone on its internal battery initially. Added electronics must not pull motor current through ESP32 GPIO or logic rails.

## 5 User experience and demonstration

### Primary flow

1. Operator powers the rover, starts the iPhone sensor app, and opens the laptop dashboard.
2. Dashboard confirms live connections, healthy tracking, valid calibration, fresh depth, and an unlatched stop state.
3. Operator creates a new session and defines a bounded demonstration area.
4. Operator explicitly arms the rover and chooses Manual, Navigate, or Explore.
5. The dashboard shows a growing point cloud or mesh, trajectory, top-down map, and object markers.
6. Operator selects an object to view its last observation, estimated position, confidence, and history.
7. Operator stops the rover, moves a distinctive prop, and initiates Rescan in the same session.
8. If the object is confidently matched, the dashboard displays a change event with old and new positions. Otherwise it reports an uncertain match.
9. Operator stops and exports the session summary.

### Interface requirements

The main view contains the 3D reconstruction, rover pose, trajectory, object labels, and selectable history. A top-down view shows free, occupied, and unknown cells plus the active path. The side panel shows connection health, tracking state, sensor age, active mode, object list, and events.

Controls: **Connect, New Session, Arm, Manual, Navigate, Explore, Rescan, Stop, Export**. Mode switches stop the rover before transferring control. Stop remains available independently of the 3D rendering workload.

Use text as well as color for states. Display an estimated object location with sensible precision; do not imply centimeter accuracy with excessive decimal places. Label commanded speed separately from pose-estimated speed. Show battery voltage only if it is actually measured. Avoid compass directions unless world-to-compass alignment has been validated; use “ahead,” “left,” or map coordinates.

## 6 Architecture and module ownership

```text
iPhone sensor app
  ARKit pose + synchronized RGB/depth + optional mesh
                         |
                   Local network
                         |
MacBook backend -------------------------------- Dashboard
  ingest and time alignment                        live map
  object detection and association                 objects/history
  world model and session storage                  mode and stop
  occupancy mapping and navigation
                         |
               short-lived drive commands
                         |
ESP32 bridge firmware
  network freshness and session checks
                         | UART
UNO motor firmware ---- local ranging / power switch
  short command lease and local watchdog
  driver outputs and motor limits
                         |
                     four motors
```

| Component | Responsibility | Suggested implementation |
|---|---|---|
| iPhone app | Capture synchronized sensor data and expose health | Swift, ARKit, URLSession WebSocket or equivalent |
| Ingestion | Validate packets, retain frame identity, bound queues | Python service |
| Perception | Detection and robust depth-based localization | Small pretrained detector; benchmark on target Mac |
| World model | Stable objects, observations, change events | Python, SQLite, versioned records |
| Navigation | Occupancy, paths, waypoints, exploration | Lightweight custom 2D implementation |
| Command broker | Sole authority for drive commands and mode changes | Backend service with explicit state machine |
| Network bridge | Wi-Fi command validation and bounded UART forwarding | C++ on included ESP32 module |
| Motor controller | Motor outputs, local stop conditions, watchdog | C++ on included UNO R3 |
| Dashboard | Live 3D scene and operational controls | TypeScript, React, Three.js |

These are implementation choices, not mandatory dependencies. Pin versions after the first successful integration. Avoid introducing ROS solely for this prototype unless the team already has a working ROS workflow.

## 7 Prioritized functional requirements

| ID | Priority | Requirement |
|---|---|---|
| SYS-01 | P0 | Start disarmed; only one command source owns motion |
| SAFE-01 | P0 | Stop on stale commands, tracking loss, stale perception, local obstacle trigger, or operator stop |
| DRIVE-01 | P0 | Execute supervised forward, reverse, rotation, and stop commands |
| SENSE-01 | P0 | Stream pose and synchronized RGB/depth with frame and session identifiers |
| MAP-01 | P0 | Display a live reconstruction using a point cloud at minimum |
| UI-01 | P0 | Show mode, tracking, connections, stale-data state, and Stop |
| LOG-01 | P0 | Save session metadata, health events, and observations for debugging |
| OBJ-01 | P1 | Detect at least three prepared object classes supported by the selected model |
| OBJ-02 | P1 | Localize observations and merge them into persistent objects |
| NAV-01 | P1 | Navigate to selected reachable waypoints in known free space |
| CHANGE-01 | P1 | Detect a confidently identified relocated prop within the same session |
| EXP-01 | P2 | Choose and visit frontiers autonomously |
| MESH-01 | P2 | Stream and update ARKit mesh anchors |
| QUERY-01 | P3 | Answer natural-language questions from structured world data |
| SESSION-02 | P3 | Restore spatial alignment across independent sessions |

P0 is the minimum viable demo. P1 is the intended strong submission. Prioritize the moved-object demonstration ahead of unrestricted frontier exploration if time becomes tight: it communicates the value of persistent memory with less navigation complexity.

## 8 iPhone sensing specification

Use one ARKit world-tracking session as the camera owner. Obtain RGB from `ARFrame.capturedImage` rather than starting an independent camera capture session. Enable scene depth after checking support; enable mesh reconstruction only after verifying support and successful point-cloud streaming. Apple documents the depth visualization workflow and separate reconstruction capability check. [Apple depth sample](https://developer.apple.com/documentation/ARKit/displaying-a-point-cloud-using-scene-depth) · [Apple reconstruction documentation](https://developer.apple.com/documentation/arkit/arworldtrackingconfiguration/scenereconstruction)

For each sensor bundle include: protocol version, session ID, map epoch, frame ID, monotonic capture timestamp, image dimensions, image orientation, camera intrinsics, camera transform, tracking state, depth dimensions and units, confidence data, and payload lengths. Pose messages may run faster than image/depth bundles, but each bundle must retain its own capture-time pose.

Start with compressed JPEG frames at 5–10 Hz and paired depth at the same rate. This simplifies frame correspondence. Move to H.264 only if bandwidth or latency testing justifies the added decoder and timestamp work. Transmit depth as binary float meters or explicitly scaled integer millimeters, not large JSON arrays. Pose/control streams must not sit behind video or mesh uploads.

Mesh messages contain anchor ID, revision, operation (`add`, `update`, `remove`), local vertices, triangle indices, and anchor transform. Replace or remove old geometry by ID; do not append every update permanently. Bound memory and decimate the visible map as it grows.

Capability failure disables the corresponding feature and prevents arming any mode that depends on it. Tracking becoming limited or unavailable stops motion. Maintain a stable thermal load by reducing image/depth rate before sacrificing pose freshness.

## 9 Frames calibration and synchronization

### Coordinate conventions

Define `T_A_B` as a transform mapping coordinates from frame B into frame A. Use meters and radians. Define these frames explicitly:

- **A:** ARKit’s session world frame.
- **C:** ARKit camera frame.
- **B:** Rover base frame, x forward, y left, z up.
- **W:** Application map frame, gravity-aligned with its origin at the rover’s initial ground-projected base position.

ARKit camera position is not the rover base position. Measure and store the rigid mounting transform `T_B_C`. Then:

```text
T_W_B = T_W_A × T_A_C × inverse(T_B_C)
```

Keep the ARKit-to-application world transform fixed for a session. Store the floor plane and mounting calibration version. If the mount moves, stop and recalibrate. ARKit’s camera transform uses a right-handed convention; its camera-axis convention must be accounted for during projection. [Apple camera transform documentation](https://developer.apple.com/documentation/arkit/arcamera/transform)

### RGB and depth alignment

Undo detector letterboxing, resizing, cropping, and image rotation before looking up depth. Scale intrinsics to the coordinate resolution actually being projected. Use the depth and pose associated with that captured frame, even when inference finishes later.

In an aligned optical frame with x right, y down, and z forward:

```text
P_optical = depth × inverse(K) × [u, v, 1]
P_world   = T_W_A × T_A_C × T_C_optical × [P_optical, 1]
```

For matching native image axes, converting optical coordinates to ARKit camera coordinates includes reversing y and z. Any additional image orientation transform must be applied consistently. Validate this physically; a point in front of the camera must appear in front in the map, and turning the rover must not make a stationary object orbit it.

### Time and reset handling

Use capture timestamps from the phone’s monotonic clock. Estimate phone-to-Mac clock offset through repeated timestamp exchanges, record uncertainty, and reject stale data. Queue length is bounded; drop old perception frames when overloaded.

Every new or reset AR session receives a new map epoch. Never combine coordinates from different epochs without verified registration. After a pose jump or suspected relocalization inconsistency, stop, suspend change detection, and re-establish alignment or start a fresh map. Persistence of saved records does not by itself provide cross-session spatial consistency.

## 10 Mapping and reconstruction

Maintain two related representations: a 3D visualization and a conservative 2D navigation map. The visual reconstruction can begin as a voxel-downsampled colored point cloud; a dense or photorealistic mesh is not required.

Use a 5 cm occupancy grid initially, increasing to 10 cm if processing becomes expensive. Determine a floor plane during startup. Classify reliable points relative to that plane; retain obstacles that intersect the rover’s full collision height, including phone overhang. Mask the rover’s own visible geometry.

Free space must be supported by observations. Ray-based clearing must respect sensor height, floor evidence, and unobserved low obstacles; a ray passing above an object does not establish floor clearance. Missing or low-confidence depth remains unknown. Combine depth evidence with the local range sensor; neither alone provides all-around obstacle coverage.

Inflate obstacles by the rover footprint, mounting overhang, and a tested clearance margin. Maintain separate static and recent-obstacle evidence so moving people do not become permanent walls. Only clear occupied cells after repeated valid free-space observations. Stop when a path is blocked and replan.

For progress, show observed area in square meters. If a percentage is needed, divide observed cells by cells in a fixed, operator-defined demo boundary and label it **observed area within demo boundary**. Do not claim a percentage of an unknown room’s reachable space.

## 11 Semantic world model and change detection

### Detection and localization

Select a small pretrained detector whose class set includes the actual props. A compact YOLO-family model is a reasonable candidate; confirm its artifact, runtime, license, and performance when selecting the implementation. Do not promise detection of arbitrary labels such as “door” without checking the chosen model’s classes.

For each box, use reliable depth samples from an inset region or segmentation mask. Reject background outliers and mixed-depth regions; prefer a robust cluster/median over a single center pixel. Require a minimum valid sample count and reasonable depth spread. If no reliable depth exists, retain a 2D observation without creating a confident 3D object.

The resulting location estimates a visible object surface or representative point, not necessarily its physical center. Store uncertainty and use consistent viewpoints for displacement tests. Promote an object after at least three consistent observations across distinct frames. These are initial tuning values.

### Persistent identity

Associate ordinary observations using class compatibility, spatial uncertainty, recency, and appearance when available. Apply one-to-one assignments within each frame; do not merge all nearby same-class objects. Save original observations separately from the smoothed current state.

A nearby-only association rule cannot identify the same backpack after it moves across the room. Add a separate rescan association step: compare unmatched new observations with historical objects using appearance, class, prior visibility, and competing candidates. Confirm only when the match is sufficiently distinct. A demo with one distinctive backpack is a reasonable first test, but uniqueness in that setup is not general object re-identification.

If identity is ambiguous, show **possible relocation** or separate new/last-seen entities. Optional fiducial tags can establish identity during development or in a disclosed tagged-object demo; do not describe tag-based identity as general visual recognition.

### Change events

Freeze a baseline observation cluster when Rescan begins. Compare it with a newly confirmed cluster, not with a moving average that already incorporates the new location. Begin with a displacement threshold of 0.6 m, raised when measured position uncertainty requires it. Require at least three consistent new observations and healthy map alignment before confirming movement.

For map drift checks, compare several static reference objects or structures. If many appear to shift together, suspend semantic movement claims until alignment is resolved.

An object outside the current field of view is **last seen**, not missing. Mark **not found on rescan** only after the prior region was reobserved with adequate visibility and without an intervening occluder. Prefer this wording to a definitive claim of removal.

### Storage records

| Record | Required fields |
|---|---|
| Session | ID, map epoch, creation time, calibration version, configuration, software versions |
| Frame | ID, capture timestamp, capture pose, intrinsics reference, tracking state, payload references |
| Observation | ID, frame ID, class, box/mask, representative position, uncertainty, detector confidence |
| World object | ID, session/epoch, class, current position, uncertainty, identity confidence, first/last seen, state |
| Change event | ID, object ID, baseline/new observation IDs, old/new positions, displacement, confidence, confirmation time |
| Health event | Timestamp, component, reason, active mode, stop state |

Keep detector confidence, geometric uncertainty, and identity confidence separate. Save immutable observations and events in SQLite. Store optional image crops and map files by reference. Raw video recording is off by default; enable it deliberately for a test session if needed.

## 12 Navigation and exploration

Use operator-selected waypoints before frontier exploration. The planner operates in known free space, with unknown space treated as unavailable for driving. A* produces a path through inflated occupancy cells; waypoint tracking converts it into forward velocity and turn rate. Permit in-place turns only when the swept footprint is clear.

For the ELEGOO base, use the vendor driver interface for independent left/right control, with motor polarity confirmed on the bench. Nominal side speeds follow `v_left = v - yaw_rate × effective_track / 2` and `v_right = v + yaw_rate × effective_track / 2`. On a four-wheel skid-steer base, effective track width must be calibrated; geometry alone does not capture tire slip. Convert requested side speeds to calibrated PWM, not assumed wheel-speed measurements. If substituting ACEBOTT later, replace this drive adapter with a verified mecanum mixer and keep lateral velocity at zero initially.

Without verified encoders, motor PWM does not measure wheel velocity. Use iPhone base pose as external feedback, calibrated PWM feedforward, low speeds, acceleration limits, and conservative heading correction. Keep telemetry honest: report measured wheel speeds only after real feedback is installed.

Begin at 0.10–0.15 m/s; allow up to 0.20 m/s only after stop and tracking tests pass. Initial angular cap is 0.5 rad/s. Use incremental turns and short segments if tire scrub or chassis vibration harms tracking. Reverse is supervised and limited; autonomous reversing and sideways motion stay disabled while rear/side observation is inadequate.

Frontier exploration selects reachable free cells adjacent to unknown cells. Prefer the nearest sufficiently large frontier, drive to a known-safe observation position, scan, and update the map. Reject targets with insufficient clearance; time out and temporarily blacklist unreachable frontiers. Finish when no reachable frontier remains or the time limit is reached, and report that condition rather than asserting complete reconstruction.

## 13 Communications and command protocol

Prefer a private local network. Use an existing access point: phone and Mac can use 5 GHz, with the ESP32 on compatible 2.4 GHz on the same LAN. Confirm there is no client isolation. If no suitable LAN exists, test an owned hotspot or the ESP32 access-point mode at reduced phone frame rates. An ESP32-hosted network is a bandwidth fallback, not a guaranteed high-throughput solution. Do not purchase a router within this baseline. Core operation should survive loss of internet connectivity.

Use separate connections for phone telemetry, bulk perception, browser updates, and ESP32 control. WebSockets are a practical first implementation. Bound all outgoing queues and coalesce drive commands so only the latest setpoint is retained. Pair clients to a session using a random local token; reject unknown clients.

### Proposed drive command

```json
{
  "version": 1,
  "session_id": "demo-01",
  "map_epoch": 1,
  "seq": 184,
  "mode": "navigate",
  "v_mps": 0.12,
  "yaw_rate_rps": 0.20,
  "issued_at_ms": 812340,
  "valid_for_ms": 250,
  "arm_token": "session-specific-token"
}
```

Transmit at 20 Hz. Sequence numbers reject duplicates and older commands. Validate token, session, active mode, finite numbers, and bounds before use. Translate sender time to controller monotonic time through a measured clock offset; reject commands whose age cannot be established within the allowed uncertainty. A receipt-based watchdog alone does not reject an old command that sat in a network queue.

The ESP32 translates network commands into a compact, bounded UART frame with sequence, signed motor setpoints, remaining lifetime, and integrity check. Large JSON packets remain on the ESP32/Mac side. Validate serial bandwidth and voltage levels against the delivered schematic; use the kit’s existing interconnect rather than wiring 5 V UNO signals directly to an unverified ESP32 input. The UNO enforces its own short command lease even if the ESP32 continues running. An old or duplicate serial frame must not renew motion. The UNO returns sequence acknowledgment, uptime, active stop reason, commanded motor outputs, front range validity, and battery voltage if supported; the ESP32 relays it to the Mac. On reconnect, discard pending motion, create a new command lease, and require explicit rearming.

### Suggested backend surface

| Interface | Purpose |
|---|---|
| `POST /sessions` | Create a new map session |
| `POST /arm` | Arm after health checks |
| `POST /stop` | Latch stopped state |
| `POST /mode` | Stop and switch operating mode |
| `POST /goal` | Validate a waypoint and request navigation |
| `POST /rescan` | Save a baseline and start revisit workflow |
| `GET /objects` and `GET /events` | Query persistent world data |
| `GET /health` | Report component and freshness status |
| `WS /live` | Dashboard telemetry and scene deltas |
| `GET /sessions/{id}/export` | Download metadata, objects, events, and map references |

## 14 Stop behavior and operating state

States: **DISARMED, READY, MANUAL, NAVIGATING, EXPLORING, PAUSED, FAULT**. A new connection never starts motion. Manual control uses a held deadman control; releasing it stops motion. Emergency or fault stops latch until an operator clears the cause and rearms.

| Trigger | Required behavior |
|---|---|
| Command expires | Zero motor command; no reuse of old setpoint |
| No valid command for 300 ms | Local watchdog disables drive outputs; 500 ms is the maximum acceptance limit |
| Phone tracking not normal | Backend stops and stops renewing the motion lease |
| Pose older than 250 ms or obstacle data older than 300 ms | Stop pending fresh, valid perception |
| Near obstacle or sensor fault in active safety path | Local stop; replan or operator intervention |
| Browser disconnect during manual mode | Deadman lease expires and rover stops |
| Backend crash, Wi-Fi loss, controller reboot | Rover becomes stationary/disarmed |
| Physical cutoff | Remove motor power independently of application software |

Implement the UNO watchdog in a nonblocking loop so UART parsing and ranging cannot stall it; independently supervise network freshness on the ESP32. A failed bridge must leave the UNO without a renewed lease and therefore stopped. Use bounded sensor timeouts. Zero PWM is not proof of instant physical braking; measure coast distance with the actual driver mode, floor, load, and battery state.

Choose obstacle margin using the tested stopping envelope: sensing/command delay travel plus braking/coasting distance plus geometric uncertainty. Start with a 0.30 m forward clearance threshold, then enlarge if testing requires it. A front ultrasonic sensor has blind zones and does not establish side, rear, glass, or drop-off safety. Use a supervised, level, bounded demo area and a spotter.

## 15 Performance and acceptance tests

All numbers below are initial acceptance targets, not vendor promises or measured results. Keep timestamps and run logs so latency claims can be checked.

| Area | Target and acceptance method |
|---|---|
| Pose | At least 20 Hz received, 30 Hz preferred; no motion with pose age above 250 ms |
| RGB and depth | At least 5 paired bundles/s; target 10 Hz under full workload |
| Detection | At least 5 processed frames/s on the chosen Mac and model |
| Semantic latency | 95th percentile capture-to-object-update below 750 ms; stretch below 500 ms |
| Dashboard | At least 20 FPS while showing the bounded demo map; target 30 FPS |
| Stop | Drive output disabled within 500 ms after command loss; record physical stopping distance separately |
| Local positioning | Representative object location within 0.30 m of a measured reference for at least 4 of 5 trials at 0.5–2.5 m range |
| Waypoint navigation | Reach three known-free goals within 0.30 m, with no contact, on three consecutive trials |
| Object persistence | Keep IDs for three props after leaving and re-entering view in at least 4 of 5 trials |
| Change demonstration | For a distinctive prop moved at least 0.8 m, confirm relocation within 3 s of reacquisition in at least 4 of 5 trials |
| Negative change test | No false movement event in five unchanged rescans of the staged scene |
| Reliability | Three consecutive 3–5 minute demonstrations without restarting code or remounting the phone |

Additional required checks:

1. Raise wheels off the floor and test direction, stop, sequence rejection, command expiry, and rearming.
2. Kill the backend and disconnect Wi-Fi while driving slowly; verify local stop.
3. Cover the camera or interrupt AR tracking; verify motion stops and map-change claims pause.
4. Rotate the rover beside a stationary prop; verify its estimated world position remains stable.
5. Test duplicate same-class props; ambiguous matches must not become confident relocation claims.
6. Reset the AR session; old-map records must not be fused into the new map epoch.
7. Introduce an obstacle into a planned route and verify stop/replan before contact.
8. Run with the complete mounted load and reduced battery charge; check brownouts, tracking, and coast distance.

## 16 Implementation schedule and ownership

The schedule assumes four contributors and a 36-hour build. Setup or coding before the event must follow its actual rules. Where prework is restricted, move those activities into the event window and reduce scope.

| Window | Main work | Exit condition |
|---|---|---|
| Hours 0–4 | Freeze protocols; verify iPhone data; assemble/flash rover; build dashboard shell | Phone emits real pose/depth; rover stops reliably |
| Hours 4–10 | Live point cloud; wireless manual driving; detector baseline | Handheld mapping and independent rover control both work |
| Hours 10–16 | Mount calibration; synchronized object localization; persistent storage | Teleoperated semantic mapping works end to end |
| Hours 16–22 | Occupancy, clearance checks, operator-selected goals | Rover reaches a waypoint and stops for an obstacle |
| Hours 22–28 | Rescan baseline, object reassociation, change visualization | Distinctive moved prop demonstrated reliably |
| Hours 28–31 | Frontier exploration only if P1 is stable | Autonomous choice of a safe observation target |
| Hours 31–36 | Feature freeze, fault tests, rehearsals, submission assets | Three complete rehearsals and recoverable fallback |

### Roles

- **iOS and sensing:** AR session, timestamps, image/depth alignment, calibration support, thermal behavior.
- **Embedded and hardware:** assembly, power, ESP32-to-UNO bridge, motor mapping, command leases, UNO watchdog, local range stop.
- **Backend and intelligence:** detector, world model, occupancy, waypoint planning, change detection.
- **Frontend and integration:** 3D display, object inspection, telemetry, mode controls, session replay, demo delivery.

The backend role is the heaviest. After hardware bring-up, the embedded contributor assists with motion calibration; after sensor streaming stabilizes, the iOS contributor assists with geometric validation. For a two-person team, commit to teleoperated mapping plus object persistence and change detection; defer autonomous exploration.

### Explicit cut points

If mounted tracking is unstable by hour 16, slow the rover and prioritize manual mapping. If waypoint navigation is unstable by hour 22, freeze autonomy and finish the semantic demonstration. At hour 31, stop adding features. A working fallback is part of the deliverable, not an improvised last-minute substitute.

## 17 Demonstration script and fallback modes

**Suggested three-minute presentation:**

1. **0:00–0:25:** Explain the problem and show the phone, inexpensive rover, and initially empty map.
2. **0:25–1:25:** Run the most reliable validated mode. Show live reconstruction, trajectory, and three object locations.
3. **1:25–1:45:** Select the backpack and show its observation history. Stop the rover before a person enters to move it.
4. **1:45–2:35:** Rescan within the same AR session; show a confirmed relocation or clearly labeled uncertain match.
5. **2:35–3:00:** Explain the actual autonomy level, limitations, and planned upgrades. Demonstrate Stop.

| Level | Demonstration | Required disclosure |
|---|---|---|
| A | Autonomous frontier exploration and semantic mapping | Target selection is autonomous |
| B | Operator selects destinations; rover follows planned paths | Waypoints are operator-selected |
| C | Manual driving with live mapping and object memory | Driving is manual |
| D | Handheld live sensing or recorded session replay | Rover unavailable or replay explicitly labeled |

Never make manual control appear autonomous. A recorded run may support a failed live demo only when plainly labeled and allowed by event rules.

## 18 Risks and mitigations

| Risk | Effect | Response |
|---|---|---|
| Phone mount shifts or vibrates | Incorrect base pose and blurred observations | Rigid mounting, slower turns, recalibration |
| Skid-steer slip under load | Poor waypoint tracking | External pose feedback, calibrated turns, shorter segments |
| ESP32 to UNO bridge failure | Lost or delayed drive commands | Freshness checks on both controllers; independent UNO watchdog |
| No wheel encoders | Uncertain motor speed and slip | Calibrated PWM; conservative motion; upgrade later |
| Wi-Fi congestion or isolation | Delayed data and commands | Private LAN, bounded queues, local watchdog |
| AR drift or relocalization | False object displacement | Session epochs, static-reference check, suspend changes |
| Duplicate similar objects | Wrong identity assignment | Appearance evidence and explicit ambiguous state |
| Reflective or low obstacles | Incomplete occupancy | Confidence filtering, local ranging, controlled props and spotter |
| Slow detection or thermal throttling | Stale semantics and reduced frame rate | Smaller model, fewer frames, separate control path |
| Driver or battery brownout | Uncommanded reset and lost connection | Mounted-load testing, supported supply, disarmed reboot |
| Too much scope | Fragile integrated demo | P0/P1 gates and explicit cut points |

## 19 Privacy data handling and truthful reporting

Operate in an authorized demonstration area. Perform core perception locally. Do not implement face recognition or identity tracking. If people are detected, treat them as temporary obstacles rather than persistent personal records. Provide a visible active-capture indicator and a session deletion action.

Keep secrets out of exported logs. If a cloud language model is added later, send only the structured records needed for the question by default. The language interface may query object locations and changes, but it must not issue unvalidated motor commands.

The submission should distinguish measured results from targets, estimated geometry from exact measurements, and demonstrated features from the roadmap. Maintain attributions for vendor examples, Apple samples, models, and other reused components.

## 20 Extension plan and change control

Preserve separate sensor, drive, perception, planner, and storage interfaces. A new chassis should require replacing the drive adapter and calibration, not rewriting the world model. Keep protocol and database schema versions explicit.

| Future addition | Prerequisite |
|---|---|
| Encoders and closed-loop wheel control | Compatible motors, wiring, shield input availability, calibrated wheel geometry |
| Mecanum chassis substitution | New drive adapter, four-wheel calibration, and observation of the swept path |
| Better object identity | Appearance model, multi-object dataset, ambiguity evaluation |
| Natural-language queries | Stable structured queries and explicit uncertainty handling |
| Multi-session maps | Verified relocalization or map registration |
| Room segmentation | Reliable geometry and a defined segmentation evaluation |
| Additional sensors | Power/current review, timestamping, sensor-to-base calibration |
| Alternate chassis | New footprint, drive adapter, load/stop tests |

For each addition, record the user-facing benefit, owner, dependency, estimate, and acceptance test. Changes that affect payload, camera placement, speed, or power trigger repeat motion and stopping tests. Changes to coordinates, timestamps, or object association trigger repeat localization and change-detection tests.

## 21 Decisions to resolve for the next revision

| Decision | Owner | Resolution needed |
|---|---|---|
| Event details | Team lead | Exact hacking window, submission rules, prework policy, hardware allowance |
| Budget and inventory | Team lead | Confirm kit contents and keep all additions within the agreed $20 cap |
| Actual phone and Mac | Sensing/backend owners | Device models, OS/Xcode compatibility, successful real-device run |
| Delivered board revision | Embedded owner | Exact UNO/ESP32/shield revisions, existing UART link, included power equipment, supported examples |
| Mechanical suitability | Embedded owner | Loaded drive test and stable phone tracking |
| Detector | Backend owner | Class coverage, artifact/license, measured throughput |
| Demo environment | Integration owner | Floor, lighting, boundary, props, network access |
| Scope commitment | Whole team | P0 baseline, P1 targets, first features to cut |

## 22 Definition of done

The baseline is complete when the rover drives wirelessly under supervision, stops on faults, streams valid pose and synchronized sensor data, and displays a live map without restarting software during a complete demo.

The intended submission is complete when it also places at least three recognizable props approximately correctly, preserves their identities out of view, reaches a selected waypoint while avoiding an obstacle, and demonstrates one verified same-session relocation with old/new positions and observation evidence.

Autonomous exploration is complete only when the rover selects its own reachable targets and executes them in the bounded test area. Passing that demonstration does not establish general autonomous operation in arbitrary indoor environments.

## 23 Reference register

The engineering thresholds, task breakdown, protocol examples, and acceptance tests above are proposed design decisions. Product facts and platform behavior are supported by the following sources, consulted September 25, 2026. Prices, included accessories, and board revisions should be checked again at purchase.

1. [ELEGOO Amazon listing — B07KPZ8RSZ](https://www.amazon.com/dp/B07KPZ8RSZ)
2. [ACEBOTT Amazon listing — B0D8PV9TY9](https://www.amazon.com/dp/B0D8PV9TY9)
3. [ELEGOO Smart Robot Car V4 manufacturer page](https://www.elegoo.com/products/elegoo-smart-robot-car-kit-v-4-0)
4. [ELEGOO official V4 software repository](https://github.com/elegooofficial/ELEGOO-Smart-Robot-Car-Kit-V4.0-New)
5. [ACEBOTT QD001 kit documentation](https://acebott.com/docs/qd001-smart-car-starter-kit-for-esp32/)
6. [ACEBOTT ESP32 controller documentation](https://acebott.com/docs/qa007-qa008-qa009-esp32-max-v1-0-controller-board/)
7. [ACEBOTT QA052 car shield documentation](https://acebott.com/docs/qa052-esp32-car-shield-v1-0/)
8. [Apple iPhone 17 Pro specifications](https://www.apple.com/ie/iphone-17-pro/specs/)
9. [Apple depth point-cloud sample](https://developer.apple.com/documentation/ARKit/displaying-a-point-cloud-using-scene-depth)
10. [Apple ARKit camera transform](https://developer.apple.com/documentation/arkit/arcamera/transform)
11. [Apple ARKit scene reconstruction](https://developer.apple.com/documentation/arkit/arworldtrackingconfiguration/scenereconstruction)
12. [Apple ARKit feature support checks](https://developer.apple.com/documentation/arkit/arconfiguration/supportsframesemantics%28_%3A%29)
13. [ELEGOO ESP32 camera-module programming guide](https://www.elegoo.com/blogs/learn/elegoo-smart-robot-car-v4-0-with-camera-upload-code-to-the-camera-module)
14. [Official ShellHacks event announcement](https://interest.shellhacks.net/)

Concept source: user-provided `message (3).txt`, titled “God’s Eye Rover.” The original remains unchanged.
