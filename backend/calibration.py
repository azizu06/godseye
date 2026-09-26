"""Measured rover geometry the map needs before it may be used for motion.

Nothing here is a default for the real car. Values come only from an explicit
calibration file (GODSEYE_ROVER_CALIBRATION) written from Tomiwa's measurements;
a value not measured yet is written as null and keeps the map not motion-ready.

- `obstacle_min_m`: the lowest obstacle height above the floor the rover cannot drive over.
- `footprint_length_m`, `footprint_width_m`: the chassis rectangle, bumpers included.
- `clearance_margin_m`: extra distance kept between the footprint and any obstacle.
- `camera_forward_m`, `camera_left_m`: where the phone camera sits relative to the
  footprint center, along the rover's forward and left axes.
- `camera_yaw_rad`: camera heading minus rover heading, positive to the left. Nothing
  in the backend applies it yet; a follower that steers by the camera yaw needs it.
- `measured_by`: who measured and where the evidence is; null means unverified.

The v1 rover position is still the camera position projected onto the floor
(docs/INTERFACES.md); no rover base frame is introduced. Instead the footprint
is covered by the smallest disc around that camera point, which needs no heading.

Bounds reject typos (centimeters in a meters field, NaN, unknown keys) at load;
they are sanity limits for a small RC car, not guesses at its size.
"""
import math
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.occupancy import FLOOR_TOL_M, HEIGHT_MARGIN_M, OBSTACLE_MAX_M

ENV = 'GODSEYE_ROVER_CALIBRATION'
_MEASURED = ('obstacle_min_m', 'footprint_length_m', 'footprint_width_m', 'clearance_margin_m',
             'camera_forward_m', 'camera_left_m', 'camera_yaw_rad')


class RoverCalibration(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False, extra='forbid', frozen=True)

    # Every field is required: an unmeasured value is an explicit null, never omitted.
    version: Literal[1]
    measured_by: str | None = Field(pattern=r'\S')
    obstacle_min_m: float | None = Field(gt=0, lt=OBSTACLE_MAX_M)
    footprint_length_m: float | None = Field(gt=0, le=1)
    footprint_width_m: float | None = Field(gt=0, le=1)
    clearance_margin_m: float | None = Field(ge=0, le=.5)
    camera_forward_m: float | None = Field(ge=-1, le=1)
    camera_left_m: float | None = Field(ge=-1, le=1)
    camera_yaw_rad: float | None = Field(ge=-math.pi, le=math.pi)

    @property
    def inflation_m(self) -> float | None:
        """Radius around the camera floor point that keeps the footprint plus margin clear."""
        geometry = (self.footprint_length_m, self.footprint_width_m, self.clearance_margin_m,
                    self.camera_forward_m, self.camera_left_m)
        if any(value is None for value in geometry):
            return None
        return math.hypot(self.footprint_length_m / 2 + abs(self.camera_forward_m),
                          self.footprint_width_m / 2 + abs(self.camera_left_m)) + self.clearance_margin_m

    @property
    def blockers(self) -> tuple:
        """Why this calibration cannot back motion yet; empty when it can."""
        blockers = [f'{name}_unmeasured' for name in _MEASURED if getattr(self, name) is None]
        if self.measured_by is None:
            blockers.append('calibration_unverified')
        # Heights read up to HEIGHT_MARGIN_M low, and hits within FLOOR_TOL_M are floor,
        # so a lower threshold would ask the map to see hazards it reads as floor noise.
        if self.obstacle_min_m is not None and self.obstacle_min_m - HEIGHT_MARGIN_M <= FLOOR_TOL_M + 1e-9:
            blockers.append('obstacle_min_unsupported')
        return tuple(blockers)


def load_calibration(path) -> RoverCalibration:
    """Parse and validate a calibration file; raises OSError or ValueError, never guesses."""
    return RoverCalibration.model_validate_json(Path(path).read_text())


def calibration_from_env(environ=os.environ) -> RoverCalibration | None:
    """The calibration named by GODSEYE_ROVER_CALIBRATION, or None when it is unset."""
    path = environ.get(ENV)
    return load_calibration(path) if path else None
