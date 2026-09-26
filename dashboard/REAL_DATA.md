# Backend observations only

Issue [#49](https://github.com/azizu06/godseye/issues/49) supersedes earlier dashboard specifications that offered a local simulator or demo source in the product.

The dashboard connects to the configured backend and never generates scene observations, objects, poses, health, motion, or events locally. The default connection is `ws://localhost:8765/live` with API base `http://localhost:8765`; REST commands start disabled. Connection settings has no simulator or synthetic-feed preset. No connection failure triggers generated fallback data.

Before observations arrive, the spatial workspace is empty and waiting. The orientation grid and view controls remain usable, but there is no fabricated rover pose, view scope, trail, occupancy, object label, or scan. Received observations continue to use the frozen [wire contract](../docs/INTERFACES.md) and [capture contract](../docs/CAPTURE.md).

Disconnecting pauses capture and marks telemetry stale; it does not erase actual previously received scans or spatial memory. Same-map reconnection resumes the retained scan after backend identity confirmation. A different backend, session, or map epoch clears the previous spatial state according to [PERSISTENT_SCAN.md](PERSISTENT_SCAN.md).

Spatial memory, object intelligence, activity, rescan, exports, view tools, and operator controls remain available. Rescan uses backend observations and never fabricates a moved object. Commands require explicit REST enablement and existing health/arming checks.

Deterministic synthetic data remains available solely in automated test fixtures and standalone validation tools. It is not imported by the production app or selectable in the product. The protocol does not authenticate sensor provenance: the dashboard cannot establish whether an arbitrary operator-configured server supplied genuine hardware measurements. This change removes frontend-generated observations; it introduces no new wire format or provenance guarantee.

Validation checks startup without a feed, absence of demo choices, received data rendering, disconnect/reconnect behavior, and command safety using isolated protocol fixtures. Tests must not contact the live hardware backend.
