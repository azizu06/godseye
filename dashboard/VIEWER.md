# Observable scan viewer

The mission dashboard combines its retained appearance-aware reconstruction with the faster capture/point path from `sais-ideal-dashboard-type-shi` (`0658e89`). Objects, occupancy, mission controls, calibrated navigation gates and the received-data-only policy in [REAL_DATA.md](REAL_DATA.md) remain in place.

## Points and surfaces

The viewer retains up to **2,000,000 measured points** in a worker-owned 1 cm association cache. Positions remain measured world coordinates; association does not snap them to voxel centers. The point renderer reuses position/color buffers and uploads changed ranges. Native RGB-D measurements supplement the negotiated `/live` feed, including isolated measurements that cannot support a triangle. Default v1 JSON and opt-in `godseye.points.v2` binary packets are both supported; a rejected subprotocol reconnects using the legacy handshake.

The persistent mesh keeps main's **1,000,000-triangle / 500,000-vertex** budget, planar compression with preserved boundaries/holes, bounded appearance refinement, and explicit capacity behavior. See [PERSISTENT_SCAN.md](PERSISTENT_SCAN.md). Supported coarse cells refine to native depth around curvature, holes and depth breaks instead of flattening or discarding the entire cell. Medium/high-confidence depth is accepted; unsupported regions stay empty. No topology is inferred from an unorganized point-only feed.

A captured image and its dense points appear before persistent fusion completes. After a patch is successfully retained, its **actual compressed triangles** retire matching coplanar points from the GPU draw list. Tests require a point inside a triangle and within 5 mm of its plane; foreground and holes remain. Coverage work is bounded to two million raster candidates per frame; reaching that limit leaves extra points visible. Coverage is local to revisited capture samples, not a full two-million-point search. Skipped or rejected geometry cannot retire new points. Global mesh coarsening restores point visibility before applying subsequent coverage. Turning off surfaces displays the retained point cache again.

Under-budget mesh integration indexes existing vertices/faces once and sends only appended positions/colors/indices to reusable GPU buffers. Capacity coarsening may rebuild the map and sends a replacement. Cancelled worker replies still drain their buffer deltas so later appends stay aligned. The geometry budgets, support checks, eight-second cooperative coarsening deadline and thirty-second outer worker timeout remain bounded.

Recent image textures retain up to 24 views / 48 MiB at a 1280-pixel longest edge. The viewport reports retained point count, triangles, **dots drawn**, layer visibility, feed and capture status separately. Surface counts include retained geometry plus recent textured overlays, so overlapping faces can be counted twice.

## Feed and capture selection

`live` and `api` URL parameters preserve the feed selection without persisting REST command permission. Without `api`, the HTTP(S) base derives from the WebSocket origin. `VITE_LIVE_URL` supplies a shared default when no explicit query overrides it.

For several browsers using one dashboard laptop, copy [.env.example](.env.example) to `.env.local`:

```dotenv
VITE_LIVE_URL=/live
GODSEYE_BACKEND_URL=http://<phone-backend-IP>:8765
```

Restart Vite after configuration changes. The phone keeps `ws://<phone-backend-IP>:8765/phone`; browsers use the dashboard laptop URL. The development and preview relay forwards an explicit set of existing dashboard routes: `/live`, `/capture`, `/health`, `/autonomy`, `/device`, mission commands, `/route`, `/voice` (including ask/confirm), `/nav` proposal/confirmation/cancel/move actions, `/events`, event audio preparation, and `/audio/<64-hex>.wav` playback. Other backend paths are not exposed. Request bodies (including raw microphone audio), response errors and audio range headers pass through unchanged. The iPhone adapter requires its pairing key for setup and motion commands; Stop remains available without it. Enable REST commands and enter the key in Connection settings, then use Rover controls for mounted-phone setup. Explicit pairing is remembered in session storage for the same tab and backend across reloads. Disabling REST commands clears it; changing either backend address does not inherit it. Arming and movement are never restored. Production hosting needs equivalent reverse-proxy routes. See [autonomy setup](../docs/AUTONOMY.md).

RGB-D polling leaves **50 ms between completed jobs**, with one network/decode/fusion operation in flight. This is a scheduling target, not measured capture/display FPS. The preferred conditional `/capture/surface.bin` contains unchanged same-frame JPEG, raw depth and confidence bytes, excluding unrelated full-sensor sections. ETag/304 responses skip unchanged data. Older servers fall back to `/capture/status` plus rich or v1 frame packets; a native frame is preferred only when within 35 ms of the newest v1 capture. A failed fusion can retry the same capture.

Decoding, image sampling and fusion run off the UI thread. Transfers stay under 32 MiB. Capture eligibility permits up to 15 seconds after backend receipt independently of drive health, and labels observations older than three seconds as delayed. Decoders still require the capture's own normal tracking, rigid calibrated transform, depth/confidence and confirmed session/epoch. Transfers or decodes that expire are rejected; old captures before known tracking loss cannot enter reconstruction.

## Received detections

The top-right **Received detections** panel shows the newest backend `detections` message: detector source, phone frame number, capture wall time, and `LIVE` or `STALE` with the time since this viewer received it (stale after 3 s). Boxes, labels and confidence are drawn only over that frame's own JPEG from `/capture/detections.jpg`; if the image for a newer frame is refused, the previous frame keeps only its own boxes and the list below names the newer frame. Each listed box says whether the same capture's depth placed it (`3D placed · depth`) or it is `2D only`. Fresh placed detections also appear in the 3D view as a `LIVE · class %` marker at their measured position; stale output, 2D-only boxes and a previous map place nothing; when a person's LIVE marker ends, its retained marker stays (see Retained people). A map reset or reconnect clears the panel. Browser coverage: `dashboard/tests/detections.spec.ts`.

## Retained people

A person the detector placed with same-frame depth keeps one retained 3D marker (`src/personMemory.ts`) after its `LIVE` marker ends: a wireframe sphere at the **latest measured position**, a dashed stem to the confirmed floor, and `PERSON · last detected … · not live`. Looking away, an empty or missed detector frame, occlusion, the 3 s LIVE expiry, disconnects and elapsed time never remove it. It is removed only when the surface worker's fresh-depth proof (the same two newer, same-map, calibrated, confidence-2 views that retire moving silhouettes, see [PERSISTENT_SCAN.md](PERSISTENT_SCAN.md#fresh-depth-cleanup-of-moving-silhouettes)) sees through the whole remembered extent (±0.25 m horizontally, ±0.35 m vertically) and both views are newer than that measurement. Any corner out of view, missing or medium-confidence depth, or anything nearer, including the person still standing there, keeps it. A cleared marker means that old spot was observed empty, not that the person's new location or the rest of the room is known. A map (source/session/epoch) change clears retained people; a same-map reconnect keeps them.

While detections keep arriving, a marker follows its person: a detection within 0.5 m plus 1.5 m/s of elapsed time (at most 2 m, only within 2 s of the last measurement) updates it, nearest first and one detection per marker, so walking leaves no trail. After 2 s unseen only a same-place (0.5 m) detection updates it; a sighting elsewhere adds a new marker and the old spot waits for its own proof. This is local association, not identity tracking: people crossing can swap markers. At most 64 retained people are kept per map; past that the oldest measurement retires. Backend `person` records linked by these detections are represented by the markers rather than drawn again as running-mean boxes; Spatial memory, the inspector, voice and `/route` still use every backend record. Retained markers are display history only and never feed navigation, approach routes or collision evidence. Browser coverage (production build): `dashboard/tests/hallwayRender.spec.ts`.

## Floor-relative overlays

The scan keeps measured ARKit world coordinates. The grid, rover/phone glyph and its labels, LiDAR scope, trajectory, navigation path, occupancy plane, approach-route drawing and the goal/route-start picking planes sit at the backend occupancy `floor_y` plus their existing small offsets (`src/floor.ts`). A missing, non-finite or out-of-range floor is shown as `Floor unknown · overlays at AR origin height` and keeps the previous Y = 0 placement; nothing is levelled, rotated or calibrated. The floor clears with the map and survives a same-map reconnect. The rover glyph remains an illustrative camera-position marker, not a calibrated chassis pose.

## Stored object labels

The 3D view applies a display-only evidence policy (`src/objectDisplay.ts`) to stored objects. An object is drawn when it was seen in at least 2 frames with confidence of at least 50%. A `person` below that bar is still drawn as `Person?` with a dashed border, so a person seen once stays discoverable. Other low-evidence objects are hidden in 3D, and the Scene settings count says how many. **Scene layers → Low-evidence objects** draws them all, and a selected object is always drawn. Spatial memory lists every stored object with its confidence, frame count and evidence note. The backend, `/objects` and voice grounding are unchanged: hiding a detection does not make it false, and showing one does not make it right.

A `present` object not re-observed for 30 s reads `Last seen Nm ago` in the label, list and inspector, never `Present`. Overlapping 3D labels yield by priority: selected, then live detections, then people, then confidence × frames. Rover, route and previous-location labels are never hidden, and a hidden label returns when zoom separates it. Browser coverage: `dashboard/tests/labelClarity.spec.ts`.

## Suggested approach route

In Object intelligence, a remembered `person` offers **Suggest approach route**. The operator then clicks the entrance or start point on the floor. The backend `/route` plan is drawn as an amber line from `START · operator-selected` to `APPROACH POINT · suggested`, with a card stating the length, the walker clearance assumptions, observed-free-only planning, and that the route is unverified and not a rover path. New occupancy evidence or a moved person hides the line until the route is rechecked. Disconnects, stale telemetry, an unavailable phone, an unconfirmed map, or a person marked last-seen/not-found or not observed within 30 seconds retire the line and explain why in the existing card. A reconnect requires a new object snapshot before rechecking; retained object memory alone does not restore the route. Late responses cannot restore a retired route, and changing the source clears it. An unsupported route shows `Route unavailable` with the reason and draws nothing. A map reset clears it. It never sends a goal or motion. Browser coverage: `dashboard/tests/route.spec.ts`.

## Lifecycle and validation

Disconnect preserves geometry, clears current pose/health/path and pauses capture until fresh map identity confirms continuity. Same-map reconnect accepts restarted point IDs while voxel association avoids duplicate dots. A changed source/session/epoch clears both point and surface workers and invalidates pending work. First observations frame automatically; **F / Frame scan** recomputes bounds on demand instead of scanning the entire map on every update. Left drag pans, right drag rotates, and wheel zooms. Historical geometry is not live navigation evidence. Two distinct high-confidence fresh depth observations can retire contradicted old silhouettes from the colored map, recent photographs and retained points; unseen or occluded geometry stays. Cleanup is incremental and conservative near depth edges, and drift remains possible. See [PERSISTENT_SCAN.md](PERSISTENT_SCAN.md#fresh-depth-cleanup-of-moving-silhouettes). The semantic rescan button is not required for this automatic cleanup.

Unit coverage includes native depth holes, planar/color preservation, incremental mesh buffers, two-million-point bounds and coverage restoration. Browser suites cover early preview, delayed captures, source/reset races, reconnects, retained color and mission lifecycle. `backend.tests.test_surface_preview` and `test_map_transport` cover exact preview bytes, freshness, binary negotiation and v1 compatibility. Swift core and backend contract tests cover capture/upload scheduling. These synthetic checks do not establish sustained physical-phone FPS or wireless throughput. The default hardware adapter remains logging-only and cannot arm.

Run `node dashboard/tools/benchmark-retained-map.mjs` from the repository root after installing dashboard dependencies to compare against main at `ed940f8` (or pass another baseline ref). The fixture accumulates 100 batches of 1,200 disconnected triangles, below capacity, and requires identical final geometry and colors. On the development Mac, median integration/update preparation was 197.5 ms before and 12.5 ms after; p95 was 473.7 ms before and 32.7 ms after. Worker payloads totaled 509.0 MB before and 10.1 MB after. These are synthetic CPU and transfer measurements, not end-to-end FPS.

For the paired iPhone adapter, Arm sends an explicit request whenever commands
are enabled and no command is pending. It does not duplicate backend readiness
gates using delayed viewer telemetry; `/arm` checks current readiness and reports
rejections. The logging-only adapter retains its existing disabled-state checks.

### One-click rover startup from any dashboard browser

Set server-only `GODSEYE_ROVER_KEY_FILE` alongside `GODSEYE_BACKEND_URL` in the
ignored `.env.local`. Vite/preview injects pairing into JSON command requests,
including explicit `/nav/confirm` actions, only when the Origin host matches the
dashboard Host. The existing gate does not compare schemes or trust forwarded-origin
headers. `/operator/status` exposes only whether pairing is configured. The key
never enters the client bundle, browser storage or URLs. This grants operator
controls to browsers accessing this configured dashboard, so host it only on the
intended operator network. Requests from other Origin hosts are rejected.

A fresh browser automatically recognizes this server pairing. **Arm rover** calls
`/arm?prepare=true`: start capture, find/connect the single available Bluetooth
rover if necessary, enable laptop control, select Explore if no autonomous mode
was selected, wait for current readiness, then run the normal arm handshake.
Stop cancels pending preparation. Multiple discovered rovers require an explicit
selection. The iPhone must still have the app open and the rover must be powered.

During Explore, every fresh person with repeated observations appears in the existing **People found** card. All eligible people are queued for an independent suggested approach from the backend-recorded mission entry, with at most two requests in flight and no person-count cutoff. The entry stays fixed at the first successful Explore start in that session and map, rather than following the rover. Each available approach is drawn in amber in both 2D and 3D; actual shared path segments overlap and diverge along the returned paths. Selecting a person highlights their branch without hiding the others. Unreachable people remain listed with their own reason. Missing or wrong-map entry metadata never substitutes the current rover position. Legacy sessions without a recorded entry require a new session at the intended entrance. Freshness loss retires only the affected person's branch; disconnect, source and map changes invalidate the whole collection. Reconnection requires new object evidence and replanning. Explicit operator-started routes remain independent: creating or clearing one does not remove the automatic branches. Plans remain unverified and do not arm or move the rover. Browser coverage: `dashboard/tests/reconPerson.spec.ts`; queue fairness/cancellation coverage: `dashboard/src/reconPlanner.test.ts`.
