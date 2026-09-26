"""One desired rover command per arm generation, dispatched by a bounded pump.

Every stop starts a new generation, so a goal or held button from before a
stop, reset, loss, mode switch or shutdown can never be sent again, not even
after a later arm. Nothing here reaches hardware; the car adapter decides that.
"""
from dataclasses import dataclass
import logging
import math
import time

from backend.drive import CarAdapter

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
    is the only place a nonzero command reaches the car, once per call. Not
    thread-safe: call every method from the event loop.
    """

    def __init__(self, car: CarAdapter, check, stop, clock=time.monotonic, limits=MotionLimits()):
        self.car, self.check, self.stop, self.clock, self.limits = car, check, stop, clock, limits
        self.generation = 0
        self.active = False  # accepting commands: armed and not stopped since
        self.desired = None
        self.zero_pending = False  # the last zero failed; retried every tick, and nothing else is sent

    def begin(self):
        """Open a fresh generation after a successful arm; None if the car refused the zero.

        Arming zeroes too, so re-arming over a held command cannot leave a car
        that latches its last command moving.
        """
        self.halt()
        if self.zero_pending:
            return None
        self.active = True
        return self.generation

    def halt(self):
        """Invalidate every held command, then send an explicit zero."""
        self.generation += 1
        self.active = False
        self.desired = None
        self.zero()

    def zero(self):
        """True once the car accepted the zero; a failure is retried by every later tick."""
        try:
            self.car.zero()
        except Exception:
            if not self.zero_pending:
                logger.exception('car zero failed; retrying every tick')
            self.zero_pending = True
            return False
        self.zero_pending = False
        return True

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
        if self.zero_pending:
            if self.active:
                self.stop('car_error')  # halt() retries the zero
            else:
                self.zero()
            return
        command = self.desired
        if command is None:
            return
        if self.clock() >= command.expires_at or command.generation != self.generation:
            self.desired = None
            if not self.zero() and self.active:
                self.stop('car_error')
            return
        if (reason := self.check(command)) is not None:
            self.stop(reason)
            return
        try:
            self.car.send(command.v_mps, command.yaw_rate_rps)
        except Exception:
            logger.exception('car send failed')
            self.stop('car_error')
