"""Explicit, uncalibrated prototype profile. Never used by the default backend.

The planner's nominal rate requests select direction and a bounded prototype
power scale, NOT a measured physical speed. Forward arcs use the bridge's
restricted differential-motor command; the measured adapter remains unchanged.
Dimensions must be supplied by the operator; measured calibration stays intact.
"""
import math
from typing import Literal, ClassVar

import numpy as np

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


def filter_prototype_self_mesh(points: np.ndarray, transform: np.ndarray,
                               geometry: PrototypeGeometry, *, intrinsics: np.ndarray | None = None,
                               image_size: tuple[int, int] | None = None) -> np.ndarray:
    """Ignore near ARKit mesh that cannot be checked by the current camera.

    The mesh is unclassified and can include the phone mount or a departed
    person. ARKit may keep anchors beside the rover after their surface leaves.
    Retain distant room geometry and anything currently in the camera image;
    current RGB-D evidence is independent of this filter.
    """
    if len(points) == 0:
        return points
    relative = points - transform[:3, 3]
    forward = relative @ -transform[:3, 2]
    lateral = relative @ transform[:3, 0]
    # Five centimeters covers the mesh voxel's position quantization.
    rear_m = geometry.footprint_length_m + geometry.clearance_margin_m + .05
    half_width_m = geometry.footprint_width_m / 2 + geometry.clearance_margin_m + .05
    self_mesh = ((forward < 0) & (forward >= -rear_m) &
                 (np.abs(lateral) <= half_width_m))
    if intrinsics is not None and image_size is not None:
        camera = relative @ transform[:3, :3]
        optical = -camera[:, 2]
        width, height = image_size
        with np.errstate(divide='ignore', invalid='ignore'):
            u = intrinsics[0, 0] * camera[:, 0] / optical + intrinsics[0, 2]
            v = -intrinsics[1, 1] * camera[:, 1] / optical + intrinsics[1, 2]
        in_view = ((optical > .05) & (u >= 0) & (u < width) &
                   (v >= 0) & (v < height))
        near = np.linalg.norm(relative[:, (0, 2)], axis=1) <= geometry.inflation_m + .05
        self_mesh |= near & ~in_view
    return points[~self_mesh]


class PrototypeActuation:
    prototype = True
    blockers = ()
    stopping_distance_m = None
    warnings = (
        'Uncalibrated prototype: estimated chassis, forward-facing camera assumed.',
        'Flat-terrain exploration may cross unseen floor; detected obstacles retain footprint clearance.',
        'PWM 60–180 for forward travel and arcs, PWM 60 for pivots; speed and stopping distance are unverified.',
    )

    def __init__(self, max_pwm=180, *, variable_arc_pwm=False):
        if type(max_pwm) is not int or not 1 <= max_pwm <= 180:
            raise ValueError('prototype max PWM must be an integer in [1, 180]')
        self.max_pwm = max_pwm
        self.variable_arc_pwm = variable_arc_pwm

    def follower(self):
        return FollowerConfig(pivot_only=False, rotate_in_place_rad=1.2,
                              lookahead_m=.6, cruise_mps=.2 if self.variable_arc_pwm else .15, min_mps=.05,
                              max_yaw_rate_rps=.5)

    def command(self, v_mps, yaw_rate_rps):
        if not (math.isfinite(v_mps) and math.isfinite(yaw_rate_rps)):
            raise ValueError('nonfinite prototype command')
        if v_mps == 0 and yaw_rate_rps == 0:
            return None
        if not 0 <= v_mps <= .2 or abs(yaw_rate_rps) > .5:
            raise ValueError('prototype drive request outside motion limits')
        if not self.variable_arc_pwm:
            # Published b03 phone rejects the new optional inner_power field.
            # Keep its exact command contract unless the paired update is explicit.
            power = min(self.max_pwm, round(60 + 120 * max(0., (v_mps - .15) / .05)))
            if v_mps and abs(yaw_rate_rps) >= .15:
                return TimedMotorCommand(5 if yaw_rate_rps > 0 else 6, power)
            return TimedMotorCommand(3 if v_mps else (1 if yaw_rate_rps > 0 else 2), power)
        if not v_mps:
            return TimedMotorCommand(1 if yaw_rate_rps > 0 else 2, min(60, self.max_pwm))
        # Smooth the requested duty from approach to cruise without boosting
        # power when steering crosses the straight/arc threshold. This is an
        # uncalibrated PWM policy, not an estimate of actual motor response.
        power = min(self.max_pwm, round(60 + 120 * max(0., min(1., (v_mps - .05) / .15))))
        # Preserve small steering requests instead of switching between straight
        # and one fixed sharp arc. At the yaw request limit the inner wheel gets
        # half power; gentler bends keep both wheels closer to cruise. This is
        # a normalized duty policy, not a measured differential-drive model.
        inner = round(power * (1. - abs(yaw_rate_rps)))
        if inner == power:
            return TimedMotorCommand(3, power)
        return TimedMotorCommand(5 if yaw_rate_rps > 0 else 6, power,
                                 inner_power=max(power // 2, inner))
