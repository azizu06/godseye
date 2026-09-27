# Accumulated colored scan

Follow [SPEC.md](SPEC.md), [SURFACES.md](SURFACES.md), [DISCOVERY.md](DISCOVERY.md), [INTERFACES.md](../docs/INTERFACES.md), and [CAPTURE.md](../docs/CAPTURE.md).

The approved goal is to preserve every observed region of a room with color while bounding memory, instead of showing only a rolling window of recent camera views. Keep a cumulative, spatially compacted triangle map beneath the existing recent textured views. Bake same-frame image color at calibrated UVs into linear vertex color before releasing each camera texture. Recent views supply detail; the accumulated map remains after those views are replaced/evicted. The point layer combines retained `/live` observations and native RGB-D samples, retiring samples covered by accepted mesh faces; see [VIEWER.md](VIEWER.md) for display freshness, counts and lifecycle.

A worker owns geometry integration and spatial clustering, keeping large merges off the UI thread. Deduplicate identical observed vertices/triangles and simplify supported planar regions, retaining finer observed detail while below the 1,000,000-triangle / 500,000-vertex geometry budget. Apply spatial clustering only when that budget is exceeded; see [Capacity recovery](#capacity-recovery) for the block-local level of detail. Only observed triangle topology may contribute geometry. Preserve small disconnected observed components with representative observed geometry instead of silently erasing them. If fragmentation cannot fit the budget, retain the existing map and explicitly report capacity rather than dropping oldest regions. The scene shows accumulated-map resolution/count separately from recent-view detail. Limit each integration to one worker operation; an aborted caller must drain that operation before another can start.
The triangle allowance supports dense meshes near the 500,000-vertex limit; the independent point cache retains up to 2,000,000 measured points. Mission snapshots separately retain up to 4,000 received chunks within the same two-million-point bound.
Allow up to 30 seconds for one worker fusion at this larger budget while retaining the separate 4-second network/image deadline and one-outstanding-operation limit.

The map is scoped to source endpoint plus session/epoch. Phone loss and backend reconnect pause capture while preserving geometry; a confirmed same map resumes. New non-null map scope, explicit New session, or source change clears it and invalidates pending work. REST-command permission changes alone do not reset geometry. Pause capture until a reconnect confirms the map; cached geometry is not evidence of live tracking.

Export snapshot includes the accumulated observed positions, indices, linear colors and map resolution. Browser reload does not currently replay captures; export before reloading to retain a file. Archive import/replay and browser persistence are outside this increment. Unknown surfaces remain empty; coarser retained color does not recreate fine image texture or remove scan noise/dynamic-object ghosts.

Implementation plan:
1. `persistentSurfaceMap.ts` and tests: cumulative geometry, deduplication, bounded spatial coarsening, original-region/color coverage and explicit capacity failure.
2. `surfaceColor.ts`, worker/client and tests: calibrated image sampling/color conversion, asynchronous fusion, compact export serialization.
3. `useColorSurfaces.ts` and `useMission.ts`: retain worker/store across connection changes, confirm map identity before resuming, keep recent textures above persistent geometry.
4. `Scene.tsx`, `App.tsx`, export and docs: accumulated count/resolution, capacity status and snapshot geometry.
5. Browser regressions: exceed old view cap, retain on disconnect/reconnect/command-permission toggle, clear on source/epoch change, reject late work, verify colors/export and existing movement controls. Review PR and merge after verification.

## Observed planar compression

Dense incoming colored RGB-D regions can release persistent-map capacity before spatial coarsening by replacing their interior vertices with a fitted polygon surface.
This is geometric simplification, not semantic proof that a region is a wall.
Grow edge-connected candidates with face normals within 12 degrees of a reference normal, require at least 64 triangles and 32 vertices, then verify every vertex lies within 8 mm of the fitted plane and every face normal still meets the agreement threshold.
Preserve observed boundary loops and holes in the plane's own coordinate system, including oblique surfaces; remove only collinear boundary samples and triangulate the retained footprint rather than inventing a bounding rectangle.
Reject non-manifold, unsupported, overly noisy, curved, or insufficiently reduced candidates and retain their original mesh.

Fit a linear RGB color gradient over each accepted plane and require every original sample to differ by less than 0.02 per linear-color channel.
Retain original boundary colors so coincident seams agree; the fit and boundary interpolation each consume half of a 0.04 total linear-color error budget.
When a whole region has stronger color detail, try smaller edge-connected color regions; retain original triangles wherever the same color bound cannot be met.
This bounded color approximation can smooth slight sensor/color noise while preserving large gradients and refusing to flatten sharp posters or patterned detail.
Recent image textures remain unchanged above the accumulated map.
Only compact retained positions, colors and triangle indices count toward the existing 500,000-vertex / 1,000,000-triangle limits; no full-resolution planar evidence cache is retained.
Repeated shifted/noisy observations remain subject to normal spatial deduplication and capacity limits; this is not perfect multi-view registration or unlimited retention.
Show the accumulated map's retained vertex count in the scene when available, with raw received-point count as the fallback.

## Retained image detail

Issue #46 addresses camera texture detail disappearing after the recent-view cache evicts a view.
Before planar compression, sample the same-frame JPEG inside each supported coarse triangle, comparing quarter-edge/interior samples with interpolated corner colors in linear RGB.
Where a sampled channel differs by more than 0.06, subdivide that existing triangle and sample its new vertices; leave uniform walls and smooth gradients coarse.
Use shared edge midpoints, at most four refinement levels, and at most 6,144 vertices per normal capture frame.
If the budget is exhausted, retain the parent face rather than dropping coverage.
This is bounded appearance refinement over existing observed geometry: no new depth measurements, inferred surfaces, or filled holes.
Sparse samples cannot guarantee preservation of every pixel or arbitrary fine texture.

Color sampling/refinement and persistent integration run in the same single-flight worker.
Publish the recent calibrated texture as soon as decoding finishes, before awaiting fusion; this reduces display latency but does not increase capture/poll frequency.
Do not mark a frame integrated before fusion completes, so cancellation and transient failure can retry it.
Under-budget maps avoid spatial clustering that would immediately erase the new detail.
Export `cell_m: 0` means no spatial clustering has yet been applied; after budget coarsening, the value records the largest grid applied, not sensor accuracy or a uniform triangle size.
Budget coarsening still trades retained detail for bounded memory and may reduce small-region coverage as previously documented.
Physical motion blur, tracking drift, overlapping noisy surfaces, and moving-object ghosts remain limitations; this change does not claim to repair them.

Capacity coarsening also checks an 8-second cooperative processing deadline inside its long loops and bounds support indexing to four million bucket entries and 500,000 spatial buckets.
The 30-second caller timeout remains an outer bound; the coarsening deadline excludes initial append/planar processing and small checkpoint intervals.

## Capacity recovery

A real handheld room scan filled the 500,000-vertex budget at about one million triangles and then stopped growing with "Map capacity reached; prior scan retained".
The first whole-map coarsening built one support index over every retained triangle, exceeded the 500,000-bucket support-index bound, and failed on every later view; the worker then stopped offering views to the map.
The separate 2,000,000-point cache (`MAX_POINTS`) is a FIFO voxel ring and never sets this status.

Over budget, the map now coarsens per 1 m world-aligned block instead of globally:
- The block with the most triangles coarsens first, one rung at a time: its grid doubles from 2 cm up to 2.56 m, and the last rung keeps one observed representative per component instead of two. Each support index covers one block, so the existing index bounds hold at full room scale.
- Vertices shared with other blocks stay fixed, so block seams stay closed. The observed-support proof, hole preservation and component representatives are unchanged.
- Coarsening aims for 85% of each budget so later views append without rework. Once the map fits its hard budget it stops after 250,000 triangle visits and continues on a later integration. Completed blocks survive the 8-second deadline when they already fit. Otherwise the integration fails atomically and the prior map stays intact.
- Sparse blocks keep their full observed detail while dense, repeatedly revisited blocks coarsen. `cell_m` and the "adaptive grid up to" label report the coarsest grid applied anywhere, not a uniform resolution.
- When no block can shrink further, as with very small test budgets, the previous whole-map coarsening runs as a fallback. If that fails for any reason other than the deadline, the map reports capacity and rejects later over-budget views without repeating the work.
- The worker offers every distinct view to the map. Capacity status reflects the latest integration, or a saturated map.

On a deterministic synthetic replay at production budgets, with noisy textured revisits of walls and floor on the development Mac, each recovery took 3–6 s and ran about every 8–10 over-budget views. Later ceiling views and every earlier view remained represented. `src/persistentSurfaceMap.capacity.test.ts` is the regression test.
This bounds memory and per-integration work but does not promise unlimited detail: repeated overlapping scans progressively coarsen the densest blocks, and a room large enough to exhaust every block still reaches capacity. Export before reset remains the way to keep full-detail history.

Persistent integration also selects approximately distinct observations while every decoded recent texture still refreshes.
Compare against at most 16 successfully fused view descriptors from the same source/map: within 10 cm camera displacement and 5 degrees forward-direction change, skip fusion only if every referenced vertex and triangle centroid lies within 5 cm Euclidean distance of prior observed samples.
Any unmatched sample admits the whole frame; a stationary camera can therefore contribute newly supported depth or moved foreground surfaces.
Descriptors contain at most 12,000 probes each, clear with source/map lifecycle, and never record failed fusion.
This finite support test is approximate; sub-5 cm changes, unsampled interiors, and purely photographic changes may wait for a different view, while current textures still update.
It bounds duplicate accumulation during small camera motions but is not full registration, exact geometric coverage proof, dynamic-object removal, or unlimited revisit deduplication.

Under-budget integration maintains vertex/triangle indexes and appends only the new geometry. Worker/GPU deltas avoid repacking the retained room on each capture. Coarsening still replaces topology atomically and restores covered point visibility. Camera framing runs on first observations and explicit Frame scan; see [VIEWER.md](VIEWER.md).
