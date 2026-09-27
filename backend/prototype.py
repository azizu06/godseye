"""Explicit, uncalibrated prototype profile. Never used by the default backend.

The planner's nominal rate requests select direction and a two-step prototype
power choice, NOT a measured physical speed. Forward arcs use the bridge's
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
        'PWM 180 for clear straight and forward-arc travel; speed and stopping distance are unverified.',
    )

    def follower(self):
        return FollowerConfig(pivot_only=False, rotate_in_place_rad=1.2,
                              lookahead_m=.6, cruise_mps=.15, min_mps=.05,
                              max_yaw_rate_rps=.5)

    def command(self, v_mps, yaw_rate_rps):
        if not (math.isfinite(v_mps) and math.isfinite(yaw_rate_rps)):
            raise ValueError('nonfinite prototype command')
        if v_mps == 0 and yaw_rate_rps == 0:
            return None
        if not 0 <= v_mps <= .2 or abs(yaw_rate_rps) > .5:
            raise ValueError('prototype drive request outside motion limits')
        if v_mps and abs(yaw_rate_rps) >= .15:
            return TimedMotorCommand(5 if yaw_rate_rps > 0 else 6, 180)
        return TimedMotorCommand(3 if v_mps else (1 if yaw_rate_rps > 0 else 2),
                                 180 if v_mps >= .18 else 60)
