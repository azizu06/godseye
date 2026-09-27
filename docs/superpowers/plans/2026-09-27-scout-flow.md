# Scout Flow Implementation Plan

> **For agentic workers:** Use executing-plans; independent protocol and mission work may be delegated through dispatching-parallel-agents.

**Goal:** Early flowing obstacle detours and persistent explicitly selected Explore.

**Architecture:** Optional inner-wheel PWM in the existing autonomy relay, with
legacy arc fallback. Explore intent remains separate from temporary motor readiness.

**Tech Stack:** Python, Swift, Arduino C++.

**Spec:** `docs/superpowers/specs/2026-09-27-scout-flow-design.md`

## Global Constraints

- No invented physical calibration; prototype policy only.
- Frozen sensor wire unchanged; magnitude, permit, session and Stop validation retained.
- Use existing worktree; preserve concurrent main changes.

## Review Focus

- Missing optional wheel power retains legacy behavior.
- Null, boolean, out-of-range and wrong-direction wheel power is refused.
- Replay/disconnect still stops physical movement.
- Explicit admin cancellation wins over a pending recovery.
- Synthetic result claims never imply a physical driving test.

## Tasks

1. Wire: tests first for Swift optional `inner_power` -> ESP `D3`, legacy omission,
   invalid types and range; implement in AutonomyWire and BridgeCore, run Swift,
   host sanitizer tests and firmware build. Document protocol.
2. Backend: add `TimedMotorCommand.inner_power: int | None = None`; relay includes
   it only when supplied. Prototype computes continuous differential at unchanged
   outer PWM. Test gentle steering, bounded power, relay serialization, and run
   real packet-driven detour with fewer than 20 steering switches, yaw steps
   below 0.1 rad/s in the nominal synthetic response, no pivots or
   pre-goal slowdowns. Update benchmark model and report format compatibly.
3. Mission: reproduce navigation endings that lose Explore; fix recovery with
   bounded retries and cancellation precedence. Run lifecycle regressions and
   complete backend suite.
4. Review all changes, fix findings, run integrated suites, record evidence,
   merge into main preserving concurrent work. Build/install matching phone and
   firmware while devices are connected; verify results explicitly.

## Execution evidence

- Baseline regression: gentle commands discarded, missing inner-wheel field,
  and 51 direction switches; tests failed before implementation.
- Ruling: direction-category counts include harmless one-PWM changes around
  straight. Test actual yaw-step magnitude (<0.1 rad/s) and total variation
  (<2 rad/s) as the primary smoothness checks; retain <20 coarse switches.
  Baseline maximum jump 0.536, total 26.33; first new run 0.0655 and 0.899.
