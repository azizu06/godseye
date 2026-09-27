"""Measured speed-to-PWM conversion for the stock Uno's timed N=2 commands.

No serial/Bluetooth I/O or default rover measurements. A real adapter must also
enforce command freshness, feedback, ownership and the independent bridge stop.
"""
from bisect import bisect_left
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.navigation import FollowerConfig


class RatePoint(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False, extra='forbid', frozen=True)
    pwm: int = Field(ge=1, le=80)
    rate: float = Field(gt=0, le=5)  # m/s for translation; rad/s magnitude for turns


@dataclass(frozen=True)
class TimedMotorCommand:
    direction: int
    pwm: int
    lease_ms: int = 1500


class ActuationCalibration(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False, extra='forbid', frozen=True)
    version: Literal[1]
    measured_by: str | None = Field(pattern=r'\S')
    forward: tuple[RatePoint, ...] | None
    reverse: tuple[RatePoint, ...] | None
    left: tuple[RatePoint, ...] | None
    right: tuple[RatePoint, ...] | None
    # Verify against ARKit pose changes on the mounted rover, not vendor labels.
    positive_yaw_direction: Literal['left', 'right'] | None
    stopping_distance_m: float | None = Field(ge=0, le=2)
    stop_time_ms: float | None = Field(gt=0, le=300)

    @model_validator(mode='after')
    def ordered_measurements(self):
        for name in ('forward', 'reverse', 'left', 'right'):
            points = getattr(self, name)
            if points is None:
                continue
            if len(points) < 2:
                raise ValueError(f'{name} needs at least two measured power/rate pairs')
            if any(b.pwm <= a.pwm or b.rate <= a.rate for a, b in zip(points, points[1:])):
                raise ValueError(f'{name} power and measured rate must strictly increase')
        return self

    @property
    def blockers(self) -> tuple[str, ...]:
        names = ('measured_by', 'forward', 'left', 'right', 'positive_yaw_direction',
                 'stopping_distance_m', 'stop_time_ms')
        result = [f'actuation_{name}_unmeasured' for name in names if getattr(self, name) is None]
        if self.forward and self.forward[0].rate > .15:
            result.append('forward_low_speed_unmeasured')
        if self.left and self.right:
            low = max(self.left[0].rate, self.right[0].rate)
            high = min(.5, self.left[-1].rate, self.right[-1].rate)
            if low > high:
                result.append('turn_low_speed_unmeasured')
        return tuple(result)

    def follower(self) -> FollowerConfig:
        self.require_ready()
        return FollowerConfig(
            pivot_only=True, rotate_in_place_rad=.12,
            cruise_mps=min(.15, self.forward[-1].rate), min_mps=self.forward[0].rate,
            max_yaw_rate_rps=min(.5, self.left[-1].rate, self.right[-1].rate))

    def require_ready(self):
        if self.blockers:
            raise ValueError(', '.join(self.blockers))

    def command(self, v_mps: float, yaw_rate_rps: float) -> TimedMotorCommand | None:
        """None means Stop. Reject arcs/unmeasured rates instead of guessing a PWM.

        Linear interpolation is a measured open-loop approximation, not an
        encoder speed controller. Round toward lower power; never extrapolate.
        """
        if not (math.isfinite(v_mps) and math.isfinite(yaw_rate_rps)):
            raise ValueError('nonfinite drive command')
        if abs(v_mps) > .2 or abs(yaw_rate_rps) > .5:
            raise ValueError('drive command exceeds motion contract')
        if v_mps == 0 and yaw_rate_rps == 0:
            return None
        self.require_ready()
        if v_mps != 0 and yaw_rate_rps != 0:
            raise ValueError('stock timed control requires straight motion or an in-place turn')
        if v_mps != 0:
            name, direction = ('forward', 3) if v_mps > 0 else ('reverse', 4)
            rate = abs(v_mps)
        else:
            name = self.positive_yaw_direction
            if yaw_rate_rps < 0:
                name = 'right' if name == 'left' else 'left'
            direction, rate = (1 if name == 'left' else 2), abs(yaw_rate_rps)
        points = getattr(self, name)
        if points is None or rate < points[0].rate - 1e-9 or rate > points[-1].rate + 1e-9:
            raise ValueError(f'{name} rate outside measured range')
        index = bisect_left([p.rate for p in points], rate)
        if index == 0:
            pwm = points[0].pwm
        elif index == len(points):
            pwm = points[-1].pwm
        else:
            a, b = points[index - 1], points[index]
            pwm = math.floor(a.pwm + (b.pwm - a.pwm) * (rate - a.rate) / (b.rate - a.rate) + 1e-9)
        return TimedMotorCommand(direction, pwm)


def load_actuation(path) -> ActuationCalibration:
    return ActuationCalibration.model_validate_json(Path(path).read_text())
