# Observed color surfaces

Issue #19 continues the goals in [SPEC.md](SPEC.md), [DISCOVERY.md](DISCOVERY.md), [the project spec](../docs/spec-v0.1.md), [INTERFACES.md](../docs/INTERFACES.md), and [CAPTURE.md](../docs/CAPTURE.md).

The default 3D scene renders solid colored triangles rather than dots. Point cloud remains an independent optional layer. Orbit, pan, navigation, pose, sensor scope, trail, memory and intelligence remain available. Unknown space stays empty. Simulation is labeled and never used to fill a live feed.

Use the existing `/capture/status`, `/capture/rich/frame.bin` (preferred native JPEG), and `/capture/frame.bin` (v1 fallback) endpoints at the configured backend. Each binary frame contains synchronized JPEG, depth, confidence, scaled intrinsics and camera-to-world transform. Reconstruct a calibrated depth grid, retaining only high-confidence finite depth in 0.05–5 m. Reject triangles across depth discontinuities. Map the same-frame JPEG through image UVs. Do not mix preview images or poses from different frames. Frozen v1 control/live contracts remain unchanged.

The accumulated colored map in [PERSISTENT_SCAN.md](PERSISTENT_SCAN.md) preserves observed regions after their textures leave the recent-view cache. Bake calibrated same-frame colors into linear-sRGB vertices before eviction; integrate in a worker with a 1,000,000-triangle / 500,000-vertex budget and adaptive spatial coarsening. Preserve observed coverage and holes; report capacity without discarding the existing map when disconnected detail cannot fit.

For fine recent detail, retain up to 24 observed view patches within a 48 MiB CPU geometry/JPEG/canvas budget, replacing nearby equivalent viewpoints (under 0.25 m with direction cosine over 0.97) and dropping oldest patches at the limit. Each depth grid has at most 12,288 vertices; color textures have a maximum 1280-pixel longest side. GPU allocations are additional and bounded by the same patch/texture limits. Source, session and map changes clear geometry and invalidate pending responses. A connection loss pauses capture and retains geometry until the backend confirms whether the map is unchanged. Texture/GPU resources are released on replacement. Camera image color is already lit; render it without artificial color tinting. Existing capture endpoints are read-only and do not depend on enabling drive commands.

The simulator progressively reveals colored triangle patches from its hidden scene using the same visibility/range/occlusion rules as discovery. No complete hidden-room mesh becomes visible. A feed with only point data reports surface unavailability and retains the point-cloud option; it does not invent connectivity or imply photograph-quality capture.

Implementation sequence: (1) calibrated decoder/triangulation and simulator geometry with tests; (2) bounded capture polling and surface renderer, default/layer UX; (3) browser and real-backend fixture checks, visual inspection, independent review, PR and merge. Verification covers projection, color UV orientation, invalid data, discontinuities, identity races, progressive retention and resource bounds.

Dense observed planar regions are simplified before accumulation using normal agreement, an 8 mm plane-fit residual limit, preserved polygon boundaries/holes, and a 0.04 total linear-color error budget.
Uniform regions and smooth color gradients can release interior vertices; uncertain geometry and sharp color detail retain their observed mesh.
This is a conservative geometric approximation, not a semantic wall detector; see [PERSISTENT_SCAN.md](PERSISTENT_SCAN.md).

## Coarse coverage preview

Issue #32 prioritizes quick visible wall coverage over fine triangle density, as requested for the demo.
The simulator uses approximately 30 cm cells and examines up to 3,600 adjacent triangles per 100 ms tick, replacing the scattered 10 cm sample sweep.
New maps still start empty, and only currently in-range, in-view, unoccluded triangles accumulate.
Vertices, edge midpoints and centroids must pass the simulator visibility checks; the doorway and furniture occlusion remain intact.
Tracking loss pauses discovery and new sessions clear it.

Live RGB-D reconstruction uses a bounded approximately 3,072-vertex coarse grid, including the depth image's far edges, and accepts measured medium or high confidence.
Validate every skipped depth pixel inside each coarse cell, retaining holes from zero-confidence/missing/out-of-range data and rejecting depth discontinuities.
Triangles become larger and fusion has fewer vertices, while synchronized image UVs retain camera texture detail.
This trades fine surface and confidence precision for preview coverage and processing speed; it does not increase hardware capture rate, extend the 5 m range, or infer an unobserved wall.
The existing planar compression, accumulated-map budgets, source lifecycle and API contracts remain unchanged.
