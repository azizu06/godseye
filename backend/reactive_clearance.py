"""Pure reactive clearance regulator: a small advisory layer under the planner.

Concept re-implemented from the Instructables "Autonomous RC Car" (KyleN38, 2018,
CC BY-NC-SA 4.0): five fixed-bearing range rays (front, both front corners, both
sides) that slow the vehicle as ranges shrink and bias steering away from the
nearer side. The .ino source was not accessible (403); nothing here is copied
from it, only the high-level idea, adapted to our sensing and safety rules:

- Ranges come from the live occupancy grid/depth, not an ultrasonic bridge we
  do not have.
- Unknown or stale evidence is never treated as free. It is tracked separately
  (`unresolved`) and never contributes a clear distance.
- This module never issues a motor command, never overrides collision/footprint
  checks, and never backs the vehicle up. It returns a speed multiplier and an
  optional side suggestion for the existing planner/follower to apply under its
  own checks.
- `preferred_side` is suppressed whenever the caller has a validated route
  active: this layer must never introduce an unconditional yaw bias that steers
  off a route the planner has already committed to.

Coordinate convention: `angle_deg` is a bearing measured from the vehicle's
current heading, matching the world bearing atan2(dx, dz) used elsewhere in
this backend: 0 is straight ahead, positive is to the right, negative is to
the left.
"""
from dataclasses import dataclass
from enum import Enum

FRONT, FRONT_RIGHT, FRONT_LEFT, RIGHT, LEFT = 0.0, 45.0, -45.0, 90.0, -90.0
_FORWARD_ANGLES = (FRONT, FRONT_RIGHT, FRONT_LEFT)


class RangeState(Enum):
    FREE = 'free'
    OCCUPIED = 'occupied'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class DirectionalRange:
    """One directional reading, derived by the caller from the occupancy grid.

    `distance_m` is the observed clear distance up to the first non-free cell
    along this bearing; it is only meaningful when `state` is FREE or OCCUPIED,
    and may be `None` if the caller has no distance for that state.
    `stale` marks evidence older than the caller's freshness budget: a stale
    reading is always treated as UNKNOWN, regardless of `state`.
    """
    angle_deg: float
    distance_m: float | None
    state: RangeState
    stale: bool = False

    @property
    def effective_state(self) -> RangeState:
        return RangeState.UNKNOWN if self.stale else self.state


@dataclass(frozen=True)
class ClearanceBounds:
    """Configurable envelope. Not a calibrated physical speed/PWM model: these
    are unitless bounds on the multiplier this module returns."""
    min_speed_scale: float = 0.15
    slow_start_m: float = 0.8
    stop_scale_m: float = 0.3
    min_gap_m: float = 0.25
    side_bias_deadband_m: float = 0.10


DEFAULT_BOUNDS = ClearanceBounds()


@dataclass(frozen=True)
class ClearanceEnvelope:
    """Advisory output for the existing planner/follower.

    Never a motor command and never a replacement for the planner's own
    collision/footprint checks; the caller decides what to do with it.
    """
    speed_scale: float
    corridor_sufficient: bool
    preferred_side: str | None
    limiting_angle_deg: float | None
    unresolved: bool


def _clearance(ray: DirectionalRange | None, bounds: ClearanceBounds):
    """Returns (clearance_m_or_None, unresolved). Never returns a clearance for
    UNKNOWN/stale evidence: that would turn unknown into free."""
    if ray is None:
        return None, True
    state = ray.effective_state
    if state is RangeState.FREE:
        return (ray.distance_m if ray.distance_m is not None else bounds.slow_start_m), False
    if state is RangeState.OCCUPIED:
        return (ray.distance_m if ray.distance_m is not None else 0.0), False
    return None, True


def _side_clearance(rays, bounds: ClearanceBounds):
    values = []
    for ray in rays:
        clearance, unresolved = _clearance(ray, bounds)
        if unresolved:
            return None
        values.append(clearance)
    return min(values) if values else None


def regulate(ranges, bounds: ClearanceBounds = DEFAULT_BOUNDS, *, route_active: bool = False) -> ClearanceEnvelope:
    """Derives a speed envelope and optional side preference from directional
    range readings. `ranges` is an iterable of `DirectionalRange`; readings at
    angles this module does not use are ignored, and angles it needs but that
    are missing are treated as unresolved, never as free.

    `route_active` marks that the existing planner already has a validated
    path committed: while true, `preferred_side` is always `None`, since this
    layer must never bias steering off a route the planner has committed to.
    """
    by_angle = {r.angle_deg: r for r in ranges}

    forward = [(angle, *_clearance(by_angle.get(angle), bounds)) for angle in _FORWARD_ANGLES]
    numeric = [(angle, clearance) for angle, clearance, unresolved in forward if not unresolved]
    any_unresolved = any(unresolved for _, _, unresolved in forward)

    front_ray = by_angle.get(FRONT)
    front_unresolved = front_ray is None or front_ray.effective_state is RangeState.UNKNOWN

    if numeric:
        limiting_angle, min_clearance = min(numeric, key=lambda pair: pair[1])
    else:
        limiting_angle, min_clearance = None, 0.0

    corridor_sufficient = min_clearance >= bounds.min_gap_m and not front_unresolved

    if numeric:
        span = max(bounds.slow_start_m - bounds.stop_scale_m, 1e-9)
        t = (min_clearance - bounds.stop_scale_m) / span
        t = min(max(t, 0.0), 1.0)
        speed_scale = bounds.min_speed_scale + t * (1.0 - bounds.min_speed_scale)
    else:
        speed_scale = bounds.min_speed_scale

    preferred_side = None
    if not route_active:
        left_clearance = _side_clearance([by_angle.get(FRONT_LEFT), by_angle.get(LEFT)], bounds)
        right_clearance = _side_clearance([by_angle.get(FRONT_RIGHT), by_angle.get(RIGHT)], bounds)
        if left_clearance is not None and right_clearance is not None:
            if right_clearance - left_clearance > bounds.side_bias_deadband_m:
                preferred_side = 'right'
            elif left_clearance - right_clearance > bounds.side_bias_deadband_m:
                preferred_side = 'left'

    return ClearanceEnvelope(
        speed_scale=speed_scale,
        corridor_sufficient=corridor_sufficient,
        preferred_side=preferred_side,
        limiting_angle_deg=limiting_angle,
        unresolved=any_unresolved,
    )
