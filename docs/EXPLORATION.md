# Experimental stop-and-scan exploration

This branch is an unmerged experiment. It does not establish that the physical
rover can safely navigate a classroom. Read `SPEC.md`, `INTERFACES.md`,
`AUTONOMY.md`, and `../backend/README.md` alongside this design.

Explore chooses bounded reachable viewpoints on observed, footprint-clear floor.
Supercover rays stop at occupied cells or the first unknown boundary; calibrated
camera pitch, roll and field of view constrain predicted visibility. Paths are
limited to 0.75 m legs and the predicted view is recomputed at the actual endpoint.
Every nonzero translation or alignment command retains the arm-generation,
calibration/profile, fresh pose, recent whole-footprint and swept-clearance gates.
A limited search may choose its best evaluated view, but cannot certify exhaustion.

The rover stops, aligns, settles for 0.6 s and waits for three distinct usable
captures. Each capture's own camera transform must match the settled view and its
phone-clock capture timestamp must exceed the settled pose watermark. Queued,
replayed, blank, unsupported or drifting captures do not credit a view. Throughout
settling and capture dwell, the command is zero. Stop, scope change and generation
changes invalidate pending work.

Ground-cell gain and high-confidence 3D surface gain are separate. New surface
voxels require two corroborating captures. A one-voxel neighborhood (5 cm X/Z,
2 cm Y) tolerates depth jitter only in the stopping metric; accepted geometry,
rendering and collision evidence are unchanged. The novelty ledger is bounded at
500,000 keys; its exhaustion is partial, never zero-gain success. Counters describe
this mission's observed evidence, not a percentage of the room or image quality.

`scan_accessible_exhausted` requires actual eligible-view exhaustion plus repeated
low gain. `scan_diminishing_returns` is a heuristic: at least 12 stable views, four
heading quadrants, three separated positions and six successive low-gain views,
with no untried known-free/unknown interface. It means **scan settled**, not that
all surfaces were seen. Unresolved occlusion, truncated search, sensing failure,
64-view/300-second limits and missing clearance remain partial stop reasons.

## Offline validation and physical limitations

Run `python -m unittest backend.tests.test_exploration backend.tests.test_explore_runner`
and `python -m backend.tests.explore_room_replay /tmp/exploration-trace.json`.
The latter raycasts desks and bags in a test-only 4 by 6 m room and emits chosen
views, measured gain and terminal reasons. Its already-surveyed floor and fresh
all-around footprint clearance are explicit idealizations. Its retained-history
revisit is a metric counterfactual, not proof that restarting a production mission
restores this history. Travel and settling are simulated; this is not a car demo.

A fixed forward camera may never observe the whole inflated footprint recently
enough to pass the one-second motion gate, especially under or behind the rover.
Expected behavior is `sensing_clearance_unknown`, including in the explicitly
opted-in prototype profile. Do not add synthetic floor, extend timestamps or
substitute fixture calibration to make the physical demo move. The prototype's
operator estimates remain estimates. Floor/tabletop ambiguity and dynamic
occlusion remain sensing limitations; semantic object labels do not grant clearance.
