# Scout Navigation Implementation Plan

**Goal:** Continuous, purposeful Scout navigation with reproducible before/after evidence.
**Architecture:** Existing A* and pure pursuit remain behind Navigator; the prototype adapter retains existing packets. Current sensing and route generation have independent lifecycles. Hardware-free benchmarks close the loop through actual packet quantization.
**Spec:** docs/superpowers/specs/2026-09-27-scout-navigation-design.md
**Tech stack:** Python, NumPy, unittest; HTML canvas replay with no external dependencies.

## Constraints
Keep the v1 wire contract, measured profiles and explicit operator Stop. No invented rover measurements. Use /Users/sairamen/.venvs/godseye/bin/python. Work in the attached scout-navigation checkout. User delegated implementation decisions; proceed without staged approval gates.

## Review focus
- Latest sensing remains authoritative while old planning jobs complete.
- A nearby obstacle or missing map cannot be hidden by a background replan.
- Discrete prototype steering must behave usefully with varied synthetic speed and wheel track.
- A reached frontier or overlapping route segment must not repeatedly attract the rover.
- Measured actuation, arrival, Stop and rearm generation behavior remain compatible.

## Tasks
- [x] Baseline: existing navigation tests plus real-packet synthetic benchmark; save metrics and source fingerprints.
- [x] Motor/controller: regress power discontinuities and turns, implement continuous power and stable pivot behavior; test prototype/follower.
- [x] Planner: regress clearance and repeated-frontier selection; optimize frontier traversal and add clearance-aware costs with shortcut parity.
- [x] Runner: regress stale planning snapshots and new-obstacle replanning; separate map refresh from route work and preserve route progress.
- [x] Evidence: run identical benchmark, backend/tools tests, firmware regression, independent review; document command and limitations, show replay.

Tasks share existing FollowerConfig, PlannerConfig, PurePursuit, PrototypeActuation.command and Navigator interfaces. Optional fields preserve existing callers. Benchmark owns tools/scout_benchmark.py; controller/planner/runner edits belong to the main implementer; actuation changes may be delegated to its auditor. Record concrete adjustments and verification below.

## Results

- Baseline navigation suite: 77 tests passed before edits. Regression tests reproduced power discontinuity, early pivot exit, stale sensing during a slow plan, lost goal on a new obstacle, stale failure races and an armed tight-corner stall. Each fixed case passed after implementation.
- Final independent review found obsolete failed-plan handling and an indefinite rejected-command stall. Added separate geometry/freshness race regressions and a tight-corner arrival regression; all passed. No review findings deferred.
- Preserved default logging-only adapter, measured actuation and v1 commands. Prototype PWM interpolation is an explicit unmeasured power choice; all synthetic geometry stays in benchmark fixtures.
- Expanded scope for reproduced transport defects: 11-entry permit retention for the existing 500 ms window, queued Stop preservation, and capture-independent Stop ACK delivery. Hardware-free tests failed before fixes and passed afterward; deploying these parts requires phone/ESP updates.
- Verification before integration: backend463/463, tools23/23, Swift32/32, four firmware ASan/UBSan host suites, actual Swift fake-BLE checks, five autonomy loopback scenarios; unsigned iOS build and ESP32-S3 build succeeded.
- Benchmark nominal: corridor46.9→17.3s, corner55.9→22.0s, approach5.6→2.2s; detour previously start_blocked, now18.2s arrival. All36 synthetic response variations arrived without chassis collision. Quantized steering changes remain visible in the replay; no physical test/video claimed.
- Integration includes concurrent main commit12f1fda (ARKit floor planes); rerun combined validation before reporting completion.
