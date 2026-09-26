"""One desired rover command per arm generation, dispatched by a bounded pump.

Every stop starts a new generation, so a goal or held button from before a
stop, reset, loss, mode switch or shutdown can never be sent again, not even
after a later arm. Nothing here reaches hardware; the car adapter decides that.
"""
from dataclasses import dataclass
import logging
import math
import time

logger = logging.getLogger(__name__)
MAX_SPEED_MPS = .2  # contract hard maximums
MAX_YAW_RATE_RPS = .5
MAX_LEASE_S = .25  # the phase-2 command validity


@dataclass(frozen=True)
class MotionLimits:
    max_speed_mps: float = .15  # contract start range is 0.10-0.15 m/s
    max_yaw_rate_rps: float = .5
    lease_s: float = .25  # a command not renewed within this is zeroed (dashboard resends every 100 ms)

    def __post_init__(self):
        for value, ceiling in ((self.max_speed_mps, MAX_SPEED_MPS), (self.max_yaw_rate_rps, MAX_YAW_RATE_RPS),
                               (self.lease_s, MAX_LEASE_S)):
            if not 0 < value <= ceiling:  # also false for NaN
                raise ValueError(f'motion limit {value} outside (0, {ceiling}]')


@dataclass(frozen=True)
class Command:
    generation: int
    mode: str
    v_mps: float
    yaw_rate_rps: float
    expires_at: float


def clamp(value, limit):
    return max(-limit, min(limit, value))


class Motion:
    """`check(command)` names why the command must not be sent now, else None.

    `stop(reason)` is the app's latched stop; it must call `halt()`. `tick()`
    is the only place a nonzero command reaches the car, once per call.
    """

    def __init__(self, car, check, stop, clock=time.monotonic, limits=MotionLimits()):
        self.car, self.check, self.stop, self.clock, self.limits = car, check, stop, clock, limits
        self.generation = 0
        self.active = False  # accepting commands: armed and not stopped since
        self.desired = None

    def begin(self):
        """Open a fresh generation after a successful arm; nothing older is kept.

        Arming zeroes too, so re-arming over a held command cannot leave a car
        that latches its last command moving.
        """
        self.halt()
        self.active = True
        return self.generation

    def halt(self):
        """Invalidate every held command, then send an explicit zero."""
        self.generation += 1
        self.active = False
        self.desired = None
        self.zero()

    def zero(self):
        try:
            self.car.zero()
        except Exception:  # the command is already dropped, so no send can follow
            logger.exception('car zero failed')

    def submit(self, generation, mode, v_mps, yaw_rate_rps):
        """Replace the desired command; False when `generation` is no longer the armed one.

        A non-finite command raises ValueError after zeroing the one it replaces.
        """
        if not self.active or generation != self.generation:
            return False
        if not (math.isfinite(v_mps) and math.isfinite(yaw_rate_rps)):
            self.desired = None
            self.zero()
            raise ValueError('non-finite drive command')
        self.desired = Command(generation, mode, clamp(v_mps, self.limits.max_speed_mps),
                               clamp(yaw_rate_rps, self.limits.max_yaw_rate_rps),
                               self.clock() + self.limits.lease_s)
        return True

    def tick(self):
        command = self.desired
        if command is None:
            return
        if self.clock() >= command.expires_at:
            self.desired = None
            self.zero()
            return
        if (reason := self.check(command)) is not None:
            self.stop(reason)
            return
        try:
            self.car.send(command.v_mps, command.yaw_rate_rps)
        except Exception:
            logger.exception('car send failed')
            self.stop('car_error')
