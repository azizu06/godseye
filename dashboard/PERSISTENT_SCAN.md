# Accumulated colored scan

Follow [SPEC.md](SPEC.md), [SURFACES.md](SURFACES.md), [DISCOVERY.md](DISCOVERY.md), [INTERFACES.md](../docs/INTERFACES.md), and [CAPTURE.md](../docs/CAPTURE.md).

The approved goal is to preserve every observed region of a room with color while bounding memory, instead of showing only a rolling window of recent camera views. Keep a cumulative, spatially compacted triangle map beneath the existing recent textured views. Bake same-frame image color at calibrated UVs into linear vertex color before releasing each camera texture. Recent views supply detail; the accumulated map remains after those views are replaced/evicted. Point-cloud mode uses accumulated colored vertices when available, retaining the raw recent-points stream only as a fallback for feeds without RGB-D.

A worker owns geometry integration and spatial clustering, keeping large merges off the UI thread. Start at approximately 2 cm clustering, deduplicate overlapping observations and coarsen globally at the 1,000,000-triangle / 500,000-vertex geometry budget. Only observed triangle topology may contribute geometry. Preserve small disconnected observed components with representative observed geometry instead of silently erasing them. If fragmentation cannot fit the budget, retain the existing map and explicitly report capacity rather than dropping oldest regions. The scene shows accumulated-map resolution/count separately from recent-view detail. Limit each integration to one worker operation; an aborted caller must drain that operation before another can start. Bound coarsening to 12 iterations and 6,000,000 triangle visits per integration, with existing support-check limits unchanged, failing atomically at capacity if necessary.
The triangle allowance supports dense meshes near the 500,000-vertex limit; the raw point-only fallback retains up to 500,000 points and 1,000 chunks.
Allow up to 30 seconds for one worker fusion at this larger budget while retaining the separate 4-second network/image deadline and one-outstanding-operation limit.

The map is scoped to source endpoint plus session/epoch. Phone loss and backend reconnect pause capture while preserving geometry; a confirmed same map resumes. New non-null map scope, explicit New session, or source change clears it and invalidates pending work. REST-command permission changes alone do not reset geometry. Pause capture until a reconnect confirms the map; cached geometry is not evidence of live tracking.

Export snapshot includes the accumulated observed positions, indices, linear colors and map resolution. Browser reload does not currently replay captures; export before reloading to retain a file. Archive import/replay and browser persistence are outside this increment. Unknown surfaces remain empty; coarser retained color does not recreate fine image texture or remove scan noise/dynamic-object ghosts.

Implementation plan:
1. `persistentSurfaceMap.ts` and tests: cumulative geometry, deduplication, bounded spatial coarsening, original-region/color coverage and explicit capacity failure.
2. `surfaceColor.ts`, worker/client and tests: calibrated image sampling/color conversion, asynchronous fusion, compact export serialization.
3. `useColorSurfaces.ts` and `useMission.ts`: retain worker/store across connection changes, confirm map identity before resuming, keep recent textures above persistent geometry.
4. `Scene.tsx`, `App.tsx`, export and docs: accumulated count/resolution, capacity status and snapshot geometry.
5. Browser regressions: exceed old view cap, retain on disconnect/reconnect/command-permission toggle, clear on source/epoch change, reject late work, verify colors/export and existing movement controls. Review PR and merge after verification.
