# Camera-textured observed surfaces

This viewport adapts main's RGB-D surface renderer (`origin/main` at `372b00c`),
including its calibrated v1/v2 capture decoder, image UV convention, and linear
color baking. It keeps this branch's gray viewport, navigation controls,
two-million-point cache, and dense binary point feed.

Surfaces appear automatically when a matching, fresh RGB-D capture is available.
**P** switches between surfaces and the original points. Point-only sources still
work. **F** frames the displayed scan, including a surface-only capture.

## Geometry and image detail

The surface worker reads `/capture/status` and the matching capture stream, at
most once per 200 ms with one request/decode/integration job in flight. It prefers
native RGB from `/capture/rich/frame.bin` when current, with v1 `/capture/frame.bin`
as fallback. JPEG, depth, intrinsics, and camera transform come from the same
packet. Mismatched sessions, epochs, calibration, tracking, stale captures, and
invalid depths are rejected. The backend exposes `tracking_lost_capture` in the
capture status so a pre-loss rich frame cannot reenter after tracking recovers.

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

The retained colored mesh is divided into one-meter spatial tiles. Only tiles
touched by a frame are rebuilt and uploaded; other GPU meshes remain intact and
can be culled independently. Coincident vertices are associated using 2 mm cells;
their stored coordinates remain actual measurements. Only observed triangle
topology is retained, and repeated matching faces update rather than duplicate.
There is no repeated global mesh coarsening or full-map transfer.

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
```

Tests cover calibrated v1/v2 projection, texture orientation, openings, confidence,
depth breaks, adaptive refinement, bounded incremental tile updates, display-mode
switching, offline retention, and map resets through an isolated real backend.
Physical-phone calibration and tracking quality still need live validation.
