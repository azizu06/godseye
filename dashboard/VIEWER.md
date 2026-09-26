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

Restart Vite after configuration changes. The phone keeps `ws://<phone-backend-IP>:8765/phone`; browsers use the dashboard laptop URL. The relay exposes the configured `/live`, `/capture` and `/health` paths. To use REST mission commands, configure the actual backend API explicitly; the preview relay does not forward command routes. Production hosting needs equivalent reverse-proxy routes.

RGB-D polling leaves **50 ms between completed jobs**, with one network/decode/fusion operation in flight. This is a scheduling target, not measured capture/display FPS. The preferred conditional `/capture/surface.bin` contains unchanged same-frame JPEG, raw depth and confidence bytes, excluding unrelated full-sensor sections. ETag/304 responses skip unchanged data. Older servers fall back to `/capture/status` plus rich or v1 frame packets; a native frame is preferred only when within 35 ms of the newest v1 capture. A failed fusion can retry the same capture.

Decoding, image sampling and fusion run off the UI thread. Transfers stay under 32 MiB. Capture eligibility permits up to 15 seconds after backend receipt independently of drive health, and labels observations older than three seconds as delayed. Decoders still require the capture's own normal tracking, rigid calibrated transform, depth/confidence and confirmed session/epoch. Transfers or decodes that expire are rejected; old captures before known tracking loss cannot enter reconstruction.

## Lifecycle and validation

Disconnect preserves geometry, clears current pose/health/path and pauses capture until fresh map identity confirms continuity. Same-map reconnect accepts restarted point IDs while voxel association avoids duplicate dots. A changed source/session/epoch clears both point and surface workers and invalidates pending work. First observations frame automatically; **F / Frame scan** recomputes bounds on demand instead of scanning the entire map on every update. Left drag pans, right drag rotates, and wheel zooms. Historical geometry can retain drift and moving-object ghosts; it is not live navigation evidence.

Unit coverage includes native depth holes, planar/color preservation, incremental mesh buffers, two-million-point bounds and coverage restoration. Browser suites cover early preview, delayed captures, source/reset races, reconnects, retained color and mission lifecycle. `backend.tests.test_surface_preview` and `test_map_transport` cover exact preview bytes, freshness, binary negotiation and v1 compatibility. Swift core and backend contract tests cover capture/upload scheduling. These synthetic checks do not establish sustained physical-phone FPS or wireless throughput. The default hardware adapter remains logging-only and cannot arm.

Run `node dashboard/tools/benchmark-retained-map.mjs` from the repository root after installing dashboard dependencies to compare against main at `ed940f8` (or pass another baseline ref). The fixture accumulates 100 batches of 1,200 disconnected triangles, below capacity, and requires identical final geometry and colors. On the development Mac, median integration/update preparation was 197.5 ms before and 12.5 ms after; p95 was 473.7 ms before and 32.7 ms after. Worker payloads totaled 509.0 MB before and 10.1 MB after. These are synthetic CPU and transfer measurements, not end-to-end FPS.
