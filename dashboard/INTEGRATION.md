# Backend integration increment

Issue #14 follows dashboard PR #13 and consumes backend main through cd49799.
Read [SPEC.md](SPEC.md), [the frozen wire contract](../docs/INTERFACES.md), and [backend extensions](../backend/README.md) together.

Preserve optional session_id/map_epoch on scoped spatial messages and reset points, pose, paths, occupancy, objects, and events when identity changes.
Hydrate GET /events only when the configured external API is enabled, validate its identity against the active live map, and discard responses after a source or map change.
Deduplicate stored/live events by backend event ID and preserve chronological order and the 200-event cap.
Keep uncertain movement and new_object_id explicit in the inspector.
Use the actual rescan baseline acknowledgement; it starts observation matching and has no countdown or completion API.
Keep the logging-only car disarmed and propagate backend 409/501 errors.
Fix same-map chunk-ID reuse and stale pending spatial messages at the backend reset boundary without changing the frozen v1 schema.

Validation covers malformed optional fields, map reset with reused chunk IDs, stale history responses, stored/live event overlap, and a successful rescan acknowledgement.
The browser also connects to the current real backend and the Python fake source.
