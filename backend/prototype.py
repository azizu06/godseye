"""Explicit, uncalibrated prototype profile. Never used by the default backend.

The planner's nominal rate requests select direction and a bounded prototype
power scale, NOT a measured physical speed. Forward arcs use the bridge's
restricted differential-motor command; the measured adapter remains unchanged.
Dimensions must be supplied by the operator; measured calibration stays intact.
"""
import math
from typing import Literal, ClassVar

from backend.actuation import TimedMotorCommand
from backend.calibration import RoverCalibration
from backend.navigation import FollowerConfig


class PrototypeGeometry(RoverCalibration):
    depth_confidence: ClassVar[int] = 1
    unknown_traversable: ClassVar[bool] = True
    basis: Literal['operator_estimate'] = 'operator_estimate'

    @property
    def blockers(self):
        # Only this explicitly selected type permits estimates. Missing values
        # and unsupported obstacle thresholds still block, as in normal mode.
        return tuple(reason for reason in super().blockers if reason != 'calibration_unverified')


def prototype_geometry(length_m: float, width_m: float) -> PrototypeGeometry:
    return PrototypeGeometry(
        version=1, measured_by=None,
        footprint_length_m=length_m, footprint_width_m=width_m,
        obstacle_min_m=.065, clearance_margin_m=.1524,
        # Cover a camera anywhere within the estimated chassis rectangle.
        # These are uncertainty bounds, not a claim about its actual offset.
        camera_forward_m=length_m / 2, camera_left_m=width_m / 2,
        camera_yaw_rad=0.,  # Requires rear camera facing rover-forward.
    )


class PrototypeActuation:
    prototype = True
    blockers = ()
    stopping_distance_m = None
    warnings = (
        'Uncalibrated prototype: estimated chassis, forward-facing camera assumed.',
        'Flat-terrain exploration may cross unseen floor; detected obstacles retain footprint clearance.',
        'PWM 60–180 for forward travel and arcs, PWM 60 for pivots; speed and stopping distance are unverified.',
    )

    def follower(self):
        return FollowerConfig(pivot_only=False, rotate_in_place_rad=1.2,
                              lookahead_m=.6, cruise_mps=.2, min_mps=.05,
                              max_yaw_rate_rps=.5)

    def command(self, v_mps, yaw_rate_rps):
        if not (math.isfinite(v_mps) and math.isfinite(yaw_rate_rps)):
            raise ValueError('nonfinite prototype command')
        if v_mps == 0 and yaw_rate_rps == 0:
            return None
        if not 0 <= v_mps <= .2 or abs(yaw_rate_rps) > .5:
            raise ValueError('prototype drive request outside motion limits')
        if not v_mps:
            return TimedMotorCommand(1 if yaw_rate_rps > 0 else 2, 60)
        # Smooth the requested duty from approach to cruise without boosting
        # power when steering crosses the straight/arc threshold. This is an
        # uncalibrated PWM policy, not an estimate of actual motor response.
        power = round(60 + 120 * max(0., min(1., (v_mps - .05) / .15)))
        # Preserve small steering requests instead of switching between straight
        # and one fixed sharp arc. At the yaw request limit the inner wheel gets
        # half power; gentler bends keep both wheels closer to cruise. This is
        # a normalized duty policy, not a measured differential-drive model.
        inner = round(power * (1. - abs(yaw_rate_rps)))
        if inner == power:
            return TimedMotorCommand(3, power)
        return TimedMotorCommand(5 if yaw_rate_rps > 0 else 6, power,
                                 inner_power=max(power // 2, inner))
