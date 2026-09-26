# Camera-textured observed surfaces

This viewport adapts main's RGB-D surface renderer (`origin/main` at `372b00c`),
including its calibrated v1/v2 capture decoder, image UV convention, and linear
color baking. It keeps this branch's gray viewport, navigation controls,
two-million-point cache, and dense binary point feed.

Surfaces appear automatically when a matching, fresh RGB-D capture is available.
**P** switches between surfaces and the original points. Point-only sources still
work. **F** frames the displayed scan, including a surface-only capture.

## Geometry and image detail

The surface worker makes one conditional request to `/capture/surface.bin` per
200 ms, with one request/decode/integration job in flight. The endpoint selects a
fresh native RGB-D frame when available and sends only its JPEG, raw depth, raw
confidence, and calibration. These section bytes are **identical** to the original;
the full capture and recordings retain all sensors. Unchanged frames return 304.
The backend checks map identity, current tracking, pose freshness, and the latest
tracking loss before replying. Download/decode time counts toward the viewer's
one-second freshness limit.

On older backends without that endpoint, the worker falls back to `/capture/status`
and `/capture/rich/frame.bin` or `/capture/frame.bin`. This compatibility path also
honors `tracking_lost_capture` when exposed. JPEG, depth, intrinsics, and camera
transform always come from the same packet. Mismatched sessions, epochs,
calibration, tracking, stale captures, and invalid depths are rejected.

Like main, the renderer connects neighboring measured depth samples and applies
their camera image. It does not draw ARKit wall bounding rectangles. Changes from
main's coarse preview:

- Use **high-confidence depth only**, matching this branch's point-quality gate.
- Start with at most 3,072 coarse vertices. Every native sample inside a coarse
  cell must be valid, continuous, and within **8 mm** of its rendered triangles
  (tested in perspective-correct inverse depth).
- Refine a failed cell into native triangles instead of discarding all its nearby
  detail or flattening a protrusion. Missing depth and silhouette discontinuities
  still leave holes. Input/refinement is capped at 65,536 native depth samples.
- Retain camera textures at up to 1280 pixels on their longest side. Mipmaps and
  bounded anisotropic filtering reduce shimmer at distance and oblique angles.
- Decode JPEG, sample colors, build triangles, and integrate geometry entirely in
  a worker. Transfer changed arrays and decoded ImageBitmaps to the renderer.

The texture regression fixture includes a four-color painting, top/bottom labels,
and a doorway-shaped gap. A separate geometry test preserves a 15 mm protrusion
hidden between the coarse vertices.

## Accumulation and limits

The retained colored mesh is divided into one-meter spatial tiles. A worker sends
only changed vertex ranges and newly appended triangle indices for each touched
tile. Existing GPU buffers are reused; color/position refinements do not reupload
unchanged topology. Other tiles remain intact and can be culled independently.
Coincident vertices are associated using 2 mm cells;
their stored coordinates remain actual measurements. Only observed triangle
topology is retained, and repeated matching faces update rather than duplicate.
There is no repeated global mesh coarsening or full-map transfer.
Vertex spatial keys are calculated once per frame and reused by adjacent faces;
triangle identity and measured positions are unchanged.

Each renderer tile has a persistent typed-array mirror. Capacity grows by doubling
up to the global vertex/index limits, so allocation and full uploads happen only
on growth or surface remount. Spare capacity can approach the used array size
(with a 64-entry minimum); worker storage, the mirror, and GPU storage are separate.
Only the populated index count is drawn, and worker-computed bounds exclude unused
capacity. Dirty ranges coalesce while rendering is paused. Returning from **P**
mode rebuilds the complete view from the mirror, including updates received while
surfaces were hidden. Map changes release the old buffers and restart tile revisions.

The retained mesh is capped at **500,000 vertices**, **500,000 triangles**, and
**2,048 tiles**. At capacity, existing faces can still refine; extra retained
geometry is skipped and the status reports surface memory full. Earlier regions
are not silently evicted. The independent point cache continues up to two million.

Up to 24 recent texture views share a 48 MiB image/geometry budget; equivalent
viewpoints are replaced. Textures leaving that cache fall back to the retained
vertex colors, so older fine image detail may soften. Mipmaps add up to one third
to texture GPU memory. Source arrays, worker indexes, and GPU buffers require
additional memory. Unmount/reset releases textures and worker resources.

Disconnects retain visible geometry. A new point-feed connection or map identity
clears it consistently with this branch's existing point-cache lifecycle.
**P** does not erase either representation. A scan remains an estimate: tracking
drift and dynamic objects can leave overlapping surfaces. This is not watertight
reconstruction, object recognition, a collision map, or driving logic.

## Checks

```sh
python3 -m unittest discover -s backend/tests
cd dashboard
npm run build
npm run format:check
npm test
node tools/benchmark-surfaces.mjs
node tools/benchmark-surfaces.mjs --baseline 6517382
node tools/benchmark-surfaces.mjs --baseline a70d948 --captures ../backend/captures
# From the repository root, inspect a local recording without replaying it:
python3 tools/benchmark_capture_preview.py path/to/frame.capture
```

Tests cover calibrated v1/v2 projection, texture orientation, openings, confidence,
depth breaks, adaptive refinement, bounded incremental tile updates, display-mode
switching, offline retention, and map resets through an isolated real backend.
Physical-phone calibration and tracking quality still need live validation.

A recorded frame measured 5.44 MB in the full sensor envelope and about 0.80 MB
as a compact preview (about 85% less dashboard transfer). Sizes depend on camera
resolution, JPEG content, and optional sensors. This saves backend-to-dashboard
bandwidth; it does not reduce the phone's full-sensor upload. The surface benchmark
also exercises a 97,410-triangle frame, alongside sparse updates to a retained
450,000-triangle map. Timings measure CPU work, not physical-device FPS.

The optional `--captures` comparison reads the newest 60 local frame packets without
uploading or replaying them into a server. It respects map identity, reports rejected
frames, uses constant vertex colors to isolate geometry work, and verifies each
updated tile's complete reconstructed arrays against the requested baseline.
In a 54-valid-frame sample, worker-to-renderer surface transfer fell from 39.1 MB
to 11.3 MB (71% less), and the largest update fell from 1.86 MB to 0.40 MB.
A small refinement in the synthetic dense map fell from 104,232 bytes to 80 bytes.
These are internal geometry transfers, not network or GPU-FPS measurements.
Dirty-range bookkeeping adds some worker work (about 6% in the dense color-update
case in one run); it avoids rebuilding and uploading complete tiles on the main
thread. The benchmark reports integration and mirror-copy costs separately.
