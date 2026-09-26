"""Car adapters. No network, serial, vendor or hardware access lives here.

An adapter sends one command, sends an explicit zero, and reports `health()`
as 'ok', 'stale' or 'down'. A real adapter may only report 'ok' from verified
car feedback, never because a write succeeded.
"""
import logging
from typing import Literal, Protocol

logger = logging.getLogger(__name__)
CarHealth = Literal['ok', 'stale', 'down']


def drive(v_mps: float, yaw_rate_rps: float) -> None:
    logger.info('DRIVE STUB v_mps=%s yaw_rate_rps=%s (not transmitted)', v_mps, yaw_rate_rps)


class CarAdapter(Protocol):
    def send(self, v_mps: float, yaw_rate_rps: float) -> None: ...
    def zero(self) -> None: ...
    def health(self) -> CarHealth: ...


class LoggingCar:
    """Production default: logs through drive() and reports the car down, so arming stays refused."""

    def send(self, v_mps, yaw_rate_rps):
        drive(v_mps, yaw_rate_rps)

    def zero(self):
        drive(0., 0.)

    def health(self):
        return 'down'


class FakeCar:
    """Records every call and reports whatever health a test or smoke run sets; moves nothing."""

    def __init__(self, health: CarHealth = 'ok'):
        self.state = health
        self.calls = []  # ('send', v_mps, yaw_rate_rps) or ('zero',)
        self.error = None  # raised from send() when set

    def send(self, v_mps, yaw_rate_rps):
        if self.error is not None:
            raise self.error
        self.calls.append(('send', v_mps, yaw_rate_rps))

    def zero(self):
        self.calls.append(('zero',))

    def health(self):
        return self.state
