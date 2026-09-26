"""Command and Stop envelopes at the car-adapter boundary, against FakeCar and a hand-stepped clock.

`Receiver` models the proposed bridge/UNO acceptance rules only to show the
envelope fields are enough to apply them. It is not firmware and proves
nothing about the car's own 300 ms command-loss stop or 500 ms acceptance limit.
"""
from dataclasses import replace
import unittest

from backend.drive import FakeCar
from backend.motion import DriveCommand, DriveStop, Motion
from backend.tests.test_motion import Clock


class Receiver:
    """Proposed rules: every Stop ends its session for good, and so does losing the link.

    A session never seen before is the explicit rearm (the backend mints one
    only on arm); within a session only a strictly newer seq changes the motors.
    """

    def __init__(self):
        self.ended, self.session, self.seq = set(), None, 0
        self.motors = (0., 0.)

    def link_lost(self):
        self.ended.add(self.session)
        self.motors = (0., 0.)

    def receive(self, envelope):
        """True when the envelope was applied."""
        if isinstance(envelope, DriveStop):
            self.ended.add(envelope.session_id)
            self.motors = (0., 0.)
            return True
        if envelope.session_id in self.ended:
            return False
        if envelope.session_id != self.session:
            self.session, self.seq = envelope.session_id, 0
        if envelope.seq <= self.seq:
            return False
        self.seq = envelope.seq
        self.motors = (envelope.v_mps, envelope.yaw_rate_rps)
        return True


def faults(envelopes):
    """Contract breaches in one adapter's hand-off order; [] when well formed."""
    found, last_seq, ended, opened = [], {}, set(), []
    for envelope in envelopes:
        session = envelope.session_id
        if envelope.seq <= last_seq.get(session, 0):
            found.append(f'seq {envelope.seq} not after {last_seq[session]} in {session}')
        last_seq[session] = envelope.seq
        if isinstance(envelope, DriveStop):
            ended.add(session)
            continue
        if session is None or session in ended:
            found.append(f'command {envelope.seq} in closed session {session}')
        if session not in opened:
            if opened and opened[-1] not in ended:
                found.append(f'{session} opened before {opened[-1]} was stopped')
            opened.append(session)
        if envelope.valid_for_ms != 250 or abs(envelope.v_mps) > .15 or abs(envelope.yaw_rate_rps) > .5:
            found.append(f'command {envelope.seq} outside the limits: {envelope}')
    return found


def shape(envelopes):
    """(kind, session, seq), sessions renamed A, B, ... in order of appearance."""
    names = {None: None}
    return [('stop' if isinstance(envelope, DriveStop) else 'send',
             names.setdefault(envelope.session_id, 'ABCDEFGH'[len(names) - 1]), envelope.seq)
            for envelope in envelopes]


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.car, self.clock, self.stops = FakeCar(), Clock(), []
        self.motion = Motion(self.car, check=lambda command: None, stop=self.stop, clock=self.clock)

    def stop(self, reason):
        self.stops.append(reason)
        self.motion.halt()

    def hold(self, generation, *speeds):
        """Renew and dispatch one held command per speed, 50 ms apart, like the 20 Hz pump."""
        for v_mps in speeds:
            self.motion.submit(generation, 'manual', v_mps, 0.)
            self.motion.tick()
            self.clock.now += .05

    def test_each_arm_opens_a_fresh_session_that_its_stop_ends(self):
        self.motion.tick()
        self.assertFalse(self.motion.submit(0, 'manual', .1, 0.))  # never armed
        first = self.motion.begin()
        self.hold(first, .1, .1, .1)
        self.motion.halt()
        for _ in range(3):
            self.motion.tick()
        self.assertFalse(self.motion.submit(first, 'manual', .1, 0.))  # disarmed: nothing reaches the car
        second = self.motion.begin()
        self.hold(second, .1, .1)
        self.assertEqual(shape(self.car.envelopes), [
            ('stop', None, 1),  # arming zeroes before any session exists
            ('send', 'A', 1), ('send', 'A', 2), ('send', 'A', 3), ('stop', 'A', 4),
            ('stop', 'A', 5),  # the next arm zeroes again, then opens B
            ('send', 'B', 1), ('send', 'B', 2)])
        self.assertEqual(faults(self.car.envelopes), [])

    def test_commands_carry_the_backend_issue_time_and_lease(self):
        generation = self.motion.begin()
        self.motion.submit(generation, 'manual', .1, 0.)  # issued at 100.000 s on the backend clock
        for _ in range(3):  # resends of the unrenewed command keep its issue time
            self.motion.tick()
            self.clock.now += .05
        self.motion.submit(generation, 'manual', .1, .2)  # renewed at 100.150 s
        self.motion.tick()
        self.clock.now += .3  # the lease lapses
        self.motion.tick()
        commands = self.car.envelopes[1:]
        self.assertEqual([(c.issued_at_ms, c.valid_for_ms, c.v_mps, c.yaw_rate_rps) for c in commands],
                         [(100000, 250, .1, 0.)] * 3 + [(100150, 250, .1, .2), (100450, 250, 0., 0.)])
        self.assertTrue(all(isinstance(c, DriveCommand) for c in commands))

    def test_lease_expiry_zero_keeps_the_session_so_a_renewed_hold_resumes(self):
        generation = self.motion.begin()
        self.hold(generation, .1, .1)
        self.clock.now += .3
        self.motion.tick()  # released button: a zero command, not a Stop
        self.hold(generation, .1)  # pressed again while still armed
        receiver = Receiver()
        self.assertTrue(all(receiver.receive(envelope) for envelope in self.car.envelopes))
        self.assertEqual(shape(self.car.envelopes),
                         [('stop', None, 1), ('send', 'A', 1), ('send', 'A', 2), ('send', 'A', 3), ('send', 'A', 4)])
        self.assertEqual((self.car.envelopes[3].v_mps, self.car.envelopes[3].yaw_rate_rps), (0., 0.))
        self.assertEqual((receiver.motors, self.stops), ((.1, 0.), []))

    def test_late_duplicate_and_reordered_commands_are_refused_from_their_fields(self):
        first = self.motion.begin()
        self.hold(first, .05, .1, .15)
        startup, a1, a2, a3 = self.car.envelopes
        receiver = Receiver()
        self.assertEqual([receiver.receive(e) for e in (startup, a1, a3, a2, a3)], [True, True, True, False, False])
        self.assertEqual(receiver.motors, (.15, 0.))
        mark = len(self.car.envelopes)
        self.motion.halt()
        second = self.motion.begin()
        self.hold(second, .05)
        for envelope in self.car.envelopes[mark:]:
            receiver.receive(envelope)
        self.assertFalse(receiver.receive(a3))  # the old session is over, however late its copy arrives
        self.assertFalse(receiver.receive(replace(a3, seq=99)))
        self.assertEqual(receiver.motors, (.05, 0.))
        self.assertEqual(faults(self.car.envelopes), [])

    def test_a_stop_that_fails_is_redelivered_unchanged_before_any_new_session(self):
        first = self.motion.begin()
        self.hold(first, .1, .1)
        self.car.error = OSError('link dropped')
        with self.assertLogs('backend.motion', 'ERROR') as logs:
            self.hold(first, .1)  # the send fails, so the car_error stop's Stop fails too
            for _ in range(3):
                self.motion.tick()
            self.assertIsNone(self.motion.begin())  # no new session while a Stop is unconfirmed
        self.assertEqual(len(logs.output), 2)  # one send failure, one zero failure; retries stay quiet
        self.assertEqual(self.stops, ['car_error'])
        pending = self.motion.unsent
        self.assertIsInstance(pending, DriveStop)
        self.car.error = None
        self.motion.tick()
        self.motion.tick()
        second = self.motion.begin()
        self.hold(second, .1)
        self.assertEqual(shape(self.car.envelopes), [
            ('stop', None, 1), ('send', 'A', 1), ('send', 'A', 2),
            ('stop', 'A', 5),  # seq 3 (failed send) and 4 (failed Stop) were never handed off
            ('stop', 'A', 6), ('send', 'B', 1)])
        self.assertIs(self.car.envelopes[3], pending)
        self.assertEqual(faults(self.car.envelopes), [])

    def test_a_bridge_that_lost_the_link_resumes_only_after_a_new_arm(self):
        receiver = Receiver()
        first = self.motion.begin()
        self.hold(first, .1, .1)
        for envelope in self.car.envelopes:
            receiver.receive(envelope)
        receiver.link_lost()  # the bridge saw the drop; the backend's writes kept succeeding
        mark = len(self.car.envelopes)
        self.hold(first, .1, .1, .1)
        self.assertEqual([receiver.receive(e) for e in self.car.envelopes[mark:]], [False] * 3)
        self.assertEqual(receiver.motors, (0., 0.))
        mark = len(self.car.envelopes)
        second = self.motion.begin()  # the operator's explicit rearm
        self.hold(second, .1)
        for envelope in self.car.envelopes[mark:]:
            receiver.receive(envelope)
        self.assertEqual(receiver.motors, (.1, 0.))


if __name__ == '__main__':
    unittest.main()
