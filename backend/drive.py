"""Car adapters. No network, serial, vendor or hardware access lives here.

An adapter hands off the envelopes defined in `backend.motion`: `send(command)`
for a dispatched `DriveCommand`, and `zero(envelope)` for the backend's own
explicit zero, either a zero `DriveCommand` that keeps the armed session or a
`DriveStop` that ends it. A real adapter transmits both the same way; they are
separate calls so a backend zero stays distinguishable in fakes and logs.

`health()` reports 'ok', 'stale' or 'down'. A real adapter may only report
'ok' from verified car feedback, never because a write succeeded. Every call
runs on the event loop, so it must return promptly (do blocking I/O on the
adapter's own thread) and raise when an envelope was not handed off.
"""
import logging

logger = logging.getLogger(__name__)


def drive(v_mps: float, yaw_rate_rps: float) -> None:
    logger.info('DRIVE STUB v_mps=%s yaw_rate_rps=%s (not transmitted)', v_mps, yaw_rate_rps)


class LoggingCar:
    """Production default: logs through drive() and reports the car down, so arming stays refused."""

    def send(self, command):
        drive(command.v_mps, command.yaw_rate_rps)

    def zero(self, envelope):
        drive(0., 0.)

    def health(self):
        return 'down'


class FakeCar:
    """Records every hand-off and reports whatever health a test or smoke run sets; moves nothing."""

    def __init__(self, health='ok'):
        self.state = health
        self.calls = []  # ('send', v_mps, yaw_rate_rps) or ('zero',)
        self.envelopes = []  # every DriveCommand and DriveStop handed off, in order
        self.error = None  # raised from send() and zero() when set, like a dropped link

    def send(self, command):
        if self.error is not None:
            raise self.error
        self.calls.append(('send', command.v_mps, command.yaw_rate_rps))
        self.envelopes.append(command)

    def zero(self, envelope):
        if self.error is not None:
            raise self.error
        self.calls.append(('zero',))
        self.envelopes.append(envelope)

    def health(self):
        return self.state
