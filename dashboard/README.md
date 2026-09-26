# God's Eye 3D viewport

A full-window gray viewport for live colored RGB + LiDAR points, with a floor
grid and origin axes. There are no rover controls or dashboard panels.

## Run

Use Node.js 22 or later:

```sh
cd dashboard
npm ci
npm run dev
```

Open http://localhost:5173. Start the [backend](../backend/README.md) on port
8765 and start capture in the [iPhone app](../ios/README.md). The viewport
automatically subscribes to `ws://<viewing-page-host>:8765/live` (`wss` on HTTPS).
The phone's development capture page remains at http://localhost:8765/capture.
The status line shows whether points are live, the retained count, and feed or
tracking interruptions. An empty view means no valid points have arrived yet.

For a different backend, use `/?live=ws%3A%2F%2Flaptop-host%3A8765%2Flive`.
Use `/?live=off` for an empty viewport without a backend connection.

## Navigation

- **Right-drag:** orbit around the current view target.
- **Left-drag:** pan the target and camera together. Shift + right-drag and middle-drag also pan.
- **Scroll:** zoom in and out.
- **F:** frame retained points and walls without changing the viewing direction.
- **Home:** restore the initial camera and target.
- **Keyboard, with the viewport focused:** arrows pan, Shift + arrows orbit, and `+` / `-` zoom.
- **Touch:** one finger orbits; two fingers pan and pinch to zoom.

Click the viewport before using keyboard shortcuts. Phone pose updates do not
steer the inspection camera. The right-button context menu is suppressed.
Rendering runs on demand for point updates, navigation, and damping.

## RGB + depth pipeline

The existing backend pairs image colors with aligned high-confidence depth,
uses camera intrinsics to back-project each sample, then uses that frame's
camera-to-world pose to place it in ARKit world coordinates. See
[`backend/mapping.py`](../backend/mapping.py) and the frozen
[`docs/INTERFACES.md`](../docs/INTERFACES.md) contract. The viewport consumes the
resulting v1 `points` chunks, preserving the transmitted positions in meters.
Y is up; the reference floor is XZ, with muted red X and green Z axes. The floor
grid is a viewing aid, not measured geometry or a calibrated rover ground plane.

Points render as small opaque round splats with perspective scaling and depth
occlusion. JPEG sRGB colors are converted to linear vertex colors for correct
display. Successive frames accumulate into the same map, so you can orbit and
inspect surfaces after the phone moves away.

The preview retains up to **2,000,000 points**, with one latest measured sample
per **1 cm spatial cell**. Positions stay at their measured coordinates rather
than snapping to cell centers. When full, new cells replace older slots in a
ring; wall compaction frees slots for reuse. The status line reports replacement.
This buffer limits display memory, not the
phone's recording. The current backend emits at most 2,500 samples per chunk
at 4 Hz, using confidence 2 and depths from 0.05 to 5 meters.

## Solid walls and point allocation

The phone's full geometry capture identifies classified vertical wall planes.
The viewport polls the compact `/capture/walls` snapshot once per second and
draws each confirmed wall as a solid gray rectangle (two triangles). It applies
the plane center, iOS 16 extent rotation, and anchor-to-world transform; it does
not classify every vertical surface as a wall. ARKit refinements resize and move
rectangles, and complete snapshots remove anchors that disappear. Rebuild the
iPhone app to include the explicit `is_wall` classification field; older packets
whose classification is `wall` are also supported.

Samples within **2 cm** of a wall's measured interior are removed from the point
buffer and skipped on arrival. Border samples and objects farther from the wall
stay as colored points. The backend also filters walls **before** selecting its
2,500-point output, allocating that per-frame budget to non-wall detail. A view
containing only wall surfaces needs no redundant point chunk. Full sensor recordings
remain untouched. Rectangles do not consume the two-million-point budget.

Point removal runs in small time-limited batches; new geometry updates coalesce
without restarting unfinished work. GPU uploads cover changed ranges instead of
the entire 48 MB position/color buffer every frame. CPU spatial indexing and GPU
copies use additional memory. Wall snapshots are capped at 128 rectangles.

This is a display simplification: thin features within 2 cm of a wall can disappear,
and rectangles can fill openings inside their bounds. Removed point samples return
when rescanned if a wall is later removed or corrected. Walls are not a collision
map or proof that an entire rectangular area was observed.

Map/session changes clear old geometry before new points arrive. Duplicate,
delayed, malformed, and oversized chunks are ignored. A disconnected or stale
feed retains the scan with a non-live status; a new WebSocket connection clears
the buffer to avoid combining unrelated sources. Legacy v1 chunks without map
identity are supported only within that connection.

Depth and tracking are estimates. Camera calibration and RGB/depth alignment
matter; not every RGB pixel has an independent LiDAR measurement. This is a
sparse colored point cloud, not a fused mesh or trained Gaussian splat scene.
Drift and moving objects can leave duplicate surfaces; surface fusion, dynamic
object removal, and calibration between the phone and rover remain future work.

## Code and verification

`src/Scene.tsx` owns the freely navigable camera, grid, and geometry slot.
`src/PointCloudLayer.tsx` uploads a bounded point buffer to Three.js.
`src/pointCloud.ts` validates, accumulates, and compacts chunks; `src/usePointCloud.ts`
handles the live connection, wall polling, reconnects, and status. `src/walls.ts`
handles rectangle validation and point filtering; `src/WallLayer.tsx` renders walls.

```sh
npm run build
npm run format:check
npx playwright install chromium
python3 -m pip install -r ../backend/requirements-test.txt
npm test
```

Tests cover world positions, color conversion, bounded accumulation, map
resets, malformed data, reconnects, and rendered point updates through mocked
WebSockets. Browser tests also exercise mouse/keyboard navigation, framing,
camera independence, window resizing, and absence of rover commands. Test
fixtures never feed the real phone backend. The pipeline test launches an isolated
backend on an ephemeral local port with an in-memory database and no recording,
sends calibrated binary RGB/depth frames behind newer poses, and checks rendered
geometry in the browser. It uses `python3` by default; set `GODSEYE_PYTHON` to the
Python executable containing your backend dependencies if needed. Physical-device
visual validation is still needed for a particular phone's calibration and tracking quality.
