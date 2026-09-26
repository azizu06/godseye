# Observable scan viewer

Reconciles viewer ideas from Sai's independent branch `sais-ideal-dashboard-type-shi` at `0658e89a63129dc869855578e88fa82bde0ef39b` onto the mission dashboard. Initial main was `59482cc2fb7f3b51b6eebea4ece75adaadf94394`; integration was rebased onto `aaa7983bfcb08d3ce0efa172b6a05332d08c3a89`, including the received-data-only policy in [REAL_DATA.md](REAL_DATA.md), then onto `6347b2b` to preserve main's independent full-sensor upload opt-out. This is a selective transplant, not a merge of Sai's divergent backend or replacement UI.

The viewport always shows the configured telemetry address, connection status, retained `/live` point count, displayed surface triangle count, layer visibility and capture reason. Points are visible by default and remain `/live` observations even after an RGB-D surface map appears. The surface count includes both persistent geometry and recent textured patches rendered over it; overlapping triangles may be counted twice. No topology is inferred from points.

Use **Frame scan** or **F** to fit retained geometry. The first observations in each map are framed automatically. Home restores the overview. Orbit damping and labels work with a demand render loop. Live points use one fixed 500,000-point GPU allocation (12 MB position/color attributes), uploading appended ranges; bounded chunk eviction compacts the retained data. The existing 1,000-chunk/500,000-point limits, RGB-D reconstruction, texture retention and surface coarsening remain in place. This removes repeated point-array flattening and idle continuous rendering; no real-device performance gain is claimed.

## Feed selection

Connection settings writes `live` and `api` query parameters into the current page URL, preserving the selection on reload without localStorage. A direct link can select a feed:

```text
http://localhost:5173/?live=ws%3A%2F%2F127.0.0.1%3A8765%2Flive
```

Without `api`, derive the HTTP(S) base from the WebSocket origin. URLs must use the supported WS(S)/HTTP(S) schemes. REST command permission is never restored from a URL or browser reload. Geometry itself remains in memory and must be exported before reload. No simulator or generated fallback is reintroduced.

## Display freshness and lifecycle

Following Sai's viewer policy, a calibrated RGB-D observation may be fetched up to **15 seconds** after backend receipt, even if the latest pose heartbeat is stale or limited. Above 3 seconds the viewer explicitly says **Rendering delayed RGB + depth**. This is display eligibility only: the packet decoder still requires the capture's own normal tracking, rigid transform, calibration and valid depth/confidence; map identity must match the confirmed live feed. Reject a transfer that expires before decoding. Main's single-flight 500 ms polling and 32 MiB packet bound are unchanged.

Freshness is not drive permission. Backend pose, map freshness, rover calibration, command envelopes, explicit arming and command leases remain unchanged. A fresh rich capture upload does not refresh drive health. Historical geometry may contain drift or moving-object ghosts; rendering it does not make it current sensing evidence.

Disconnect retains live point chunks, recent textures and the accumulated surface map. Reconnect clears live pose/health/path and the per-connection chunk-ID deduplication window, then requires fresh map identity before capture resumes. The same map can accept restarted chunk IDs; repeated geometry remains bounded and may appear twice. A different source or session/epoch clears spatial state and invalidates pending capture work. The existing retained surface path remains independent of transient phone buffers.

## Integration boundaries and checks

Keep main's health, objects, events, occupancy, mission controls, reconstruction and backend/iOS contracts. Do not import Sai's 30 Hz capture profile, expanded upload/recording/binding defaults, binary dense-point protocol, compact surface endpoint, or separate tiled surface-fusion algorithm. The compact and tiled paths require a distinct backend/fusion contract and could replace main's retained appearance/detail work; they are deferred. No phone installation, physical capture, provider calls or real rover movement is needed for this viewer.

`tests/viewer.spec.ts` exercises point-only geometry, actual GPU draw range, demand framing, reconnect, layer observability, map reset, and delayed v1/v2 RGB-D with commands off. Existing surface and retention tests cover main's texture/fusion/detail behavior and identity races. `backend.tests.test_drive_safety` verifies that a viewable rich upload cannot restore stale drive authority; the full backend and Swift contract checks remain applicable. These are synthetic checks, not evidence that the owner's real scan or Wi-Fi timing problem is fixed.
