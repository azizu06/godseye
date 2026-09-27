# Scout navigation for a continuous demo

The operator requests an autonomous rover that visibly moves, turns, avoids mapped obstacles and explores, with implementation decisions delegated. Existing phone/BLE/ESP deployment remains the control path. Success is sustained route completion and fewer command discontinuities in reproducible synthetic driving scenarios, plus regression coverage of the async runner under map changes. A simulation is evidence about software, never proof of real floor response.

The implementation retains the frozen wire contract, explicitly estimated prototype geometry, measured adapter, and operator Stop. It improves the existing planner/follower and fixes two transport lifecycle defects without changing packets. Backend behavior is independently usable; permit retention and Stop delivery fixes require updated firmware/phone builds. Prototype nominal velocities remain power requests rather than measurements.

Changes:
- Continuous prototype forward power and matching arc power, full cruise when the route supports it, deliberate pivots with heading hysteresis.
- Track route progress and turn geometry; avoid chasing a carrot through an obstacle corner.
- Prefer usable obstacle clearance in path cost rather than shortest paths skimming inflated cells.
- Separate current sensing/map checks from asynchronous route planning. Replan around new obstacles and replace paths without braking for an obstacle well beyond the immediate travel horizon. Preserve explicit Stop, freshness and generation semantics.
- Speed up frontier search and remember reached frontiers so Explore does not repeatedly chase the same unmapped seam.
- Retain ESP permits for their entire existing 500 ms validity; deliver and acknowledge queued Stop independently of permit/capture gaps.
- Add a deterministic benchmark with actual prototype command quantization, explicit synthetic wheel response, metrics and a self-contained replay. Run identical before/after scenarios.

Validate actual planner/follower/runner behavior in straight corridors, a detour, right-angle turns, goal approach, changing maps, planner delays, repeated frontiers and command thresholds. Run the backend and tool regression suites. Physical video still requires a real run with the mounted phone and rover; this work must not claim to have recorded one.
