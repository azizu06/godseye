# Bounded Explore capture pacing

Explore retains the current frontier, obstacle-recovery and motion safety policy.
This change inserts a short stationary capture opportunity before the first feasible
forward departure, then only after at least 1 m translation or 60° camera rotation
and 5 s since the preceding attempt. Navigation turns and blockers take priority.

Each attempt continually submits zero, requires the measured full camera pose to
remain within 3 cm / 5° for 0.5 s, then examines distinct accepted same-frame live RGB-D
observations captured after that settled pose watermark. Three usable observations
and low added quantized 3D support permit early departure. A 2 s hard deadline bounds
the cost even if the camera drifts or data is sparse. Attempts are latched by pose;
a timeout cannot immediately restart at the same place. Motion resumes only
through the normal planner/clearance checks.

Usable support is high-confidence finite native depth, independent of point-chunk
dedup, mesh repetition and AR floor-plane augmentation. Bounded pre-dedup samples
and per-checkpoint novelty sets limit CPU/memory. Receipt-based age and same-phone
capture ordering prevent queued pre-stop frames from earning stationary credit.
Disconnect, tracking loss and map/owner changes invalidate observations.

This is a quality/time heuristic, not a sharpness or coverage certificate. It does
not measure JPEG blur, color completeness, unseen room regions or physical vehicle
speed. The explicit prototype retains its existing uncalibrated PWM mapping.
No whole-room completion is inferred. Limited evidence is diagnostic, not a new
motion authorization or terminal navigation failure.

Control-priority iOS streams still target 5–10 Hz live JPEG+raw depth; default backend
mapping integrates at 4 Hz. Bulk rich uploads are disabled under that control mode.
Three post-settle frames normally fit within 2 s; a degraded 1 Hz stream may time out,
which is reported without claiming success. No new endpoint or phone wire format
is required. Optional `/autonomy.scan_pacing` reports checkpoint state/results.

Validation uses deterministic policy clocks, actual Navigator with kinematic
motion and accepted-map transport fixtures. Required cases: cadence/phase jitter,
duplicate/old/moving captures, full-pose drift, deduplicated valid frames, failed
map commits, timeout/no-repeat, obstacle/turn preemption, real sensing loss,
Stop/reset/generation changes, and A/B stationary captures versus moving baseline.

The metric examines at most 65,536 depth pixels and 2,048 pre-dedup high-confidence
world points. A usable frame has at least 64 high-confidence finite depth samples,
at least 15% valid depth pixels, and at least 16 distinct 10 cm support cells.
Each checkpoint retains at most 8,192 such cells. After three qualifying frames,
additional support of at most eight cells or 3% of the current sample permits
release. These thresholds are bounded heuristics, not calibrated quality targets;
continued novelty or sensor noise can consume the full two-second allowance.
Only depth contributes to this metric; original map/render data is unchanged.

In the controlled 4 Hz capture, actual-Navigator A/B fixture, a 2.3 s observation
window produced zero stationary captures without pacing and five with pacing.
Both runs progressed; synthetic travel was approximately 0.37 m versus 0.16 m.
This demonstrates the intentional bounded time cost and stationary opportunities,
not improved measured hardware image quality. The test uses ideal known-floor
visibility and a kinematic command sink; physical speed and mount quality remain
unverified. A 1 Hz degraded fixture times out without reporting stable support.
