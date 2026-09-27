"""Bounded voice moves: units, limits, transcript grounding and the pose-measured runner.

Fake pose, clock, submit and stop only; nothing here describes or drives a real rover.
"""
import asyncio
import math
import time
import unittest
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.moves import MoveRequest, MoveRunner, MoveSettings, heard_problem, register_move_routes, request_problem
from backend.navigation import FollowerConfig
from backend.navigator import RoverPose
from tools.car_rehearsal import arm, scene, wait_for


class RequestTests(unittest.TestCase):
    def test_units_convert_exactly_and_never_assume_centimeters(self):
        self.assertAlmostEqual(MoveRequest('forward', 20, 'cm').target, .2)
        self.assertAlmostEqual(MoveRequest('forward', .3, 'm').target, .3)
        self.assertAlmostEqual(MoveRequest('forward', 10, 'in').target, .254)
        self.assertAlmostEqual(MoveRequest('left', 30, 'deg').target, math.radians(30))
        self.assertAlmostEqual(MoveRequest('forward', 100, 'in').target, 2.54)  # never read as 100 cm
        self.assertEqual(request_problem(MoveRequest('forward', 100, 'in')), 'move_out_of_range')

    def test_limits_are_small_and_rejected_rather_than_clamped(self):
        for request in (MoveRequest('forward', 5, 'cm'), MoveRequest('forward', 50, 'cm'),
                        MoveRequest('forward', .5, 'm'), MoveRequest('right', 10, 'deg'),
                        MoveRequest('left', 90, 'deg')):
            with self.subTest(request=request):
                self.assertIsNone(request_problem(request))
        for request in (MoveRequest('forward', 4, 'cm'), MoveRequest('forward', 51, 'cm'),
                        MoveRequest('forward', 1, 'm'), MoveRequest('forward', 100, 'cm'),
                        MoveRequest('left', 9, 'deg'), MoveRequest('right', 91, 'deg'),
                        MoveRequest('left', 360, 'deg')):
            with self.subTest(request=request):
                self.assertEqual(request_problem(request), 'move_out_of_range')

    def test_describes_what_was_said_and_its_meaning(self):
        self.assertEqual(MoveRequest('forward', 20, 'cm').label, 'forward 20 cm (0.20 m)')
        self.assertEqual(MoveRequest('forward', 10, 'in').label, 'forward 10 in (0.25 m)')
        self.assertEqual(MoveRequest('right', 30, 'deg').label, 'turn right 30°')


class HeardTests(unittest.TestCase):
    def test_the_transcript_must_contain_the_number_and_only_that_unit(self):
        for text, amount, unit in (
                ('Move forward 20 centimeters.', 20, 'cm'), ('go forward 20cm', 20, 'cm'),
                ('Drive forward twenty centimetres please', 20, 'cm'), ('forward 0.3 meters', .3, 'm'),
                ('move ahead one hundred and twenty centimeters', 120, 'cm'),
                ('go forward 10 inches', 10, 'in'), ('Turn left 30 degrees.', 30, 'deg'),
                ('turn right forty-five degrees', 45, 'deg'), ('rotate left 90°', 90, 'deg'),
                ("I'd like you to go forward 2 m", 2, 'm')):
            with self.subTest(text=text):
                self.assertIsNone(heard_problem(text, amount, unit))

    def test_a_missing_or_guessed_unit_or_number_needs_clarification(self):
        for text, amount, unit in (
                ('go 100 forward', 100, 'cm'),  # no unit said: never guessed
                ("I'm moving forward 20", 20, 'm'),  # an apostrophe m is not meters
                ('go forward 20 inches', 20, 'cm'),  # the model changed the unit
                ('move forward 1 foot', 30.48, 'cm'),  # the model converted an unsupported unit
                ('go forward half a meter', .5, 'm'),  # no number heard
                ('go forward 20 centimeters', 25, 'cm'),  # the model changed the number
                ('forward 20 cm and turn left 30 degrees', 20, 'cm'),  # more than one segment
                (None, 20, 'cm')):
            with self.subTest(text=text):
                self.assertEqual(heard_problem(text, amount, unit), 'move_unclear')


class Clock:
    def __init__(self):
        self.t = 100.

    def __call__(self):
        return self.t


class Rig:
    """A fake rover whose tracked pose follows the commands it was sent, when `responsive`."""

    def __init__(self, test, **settings):
        self.clock = Clock()
        self.x = self.z = self.yaw = 0.
        self.age, self.tracking = 0., 'normal'
        self.gone = False
        self.armed_mode = 'manual'
        self.generation = 7
        self.sent, self.stops = [], []
        self.responsive = True
        self.scale = 1.  # >1 moves faster than commanded; <0 turns the wrong way
        self.lateral = 0.
        self.coast = 0.  # extra distance after the stop, like a rover rolling on
        self.hooks = {}  # sent-command count -> what happens right after that command
        self.runner = MoveRunner(MoveSettings(**dict(dict(rate_hz=1000., settle_s=.02), **settings)),
                                 pose=self.pose, submit=self.submit,
                                 stop=self.stop, armed_mode=lambda: self.armed_mode, speeds=(.1, .4),
                                 clock=self.clock)
        test.addAsyncCleanup(self.runner.aclose)

    def pose(self):
        if self.gone:
            return None
        return RoverPose(self.x, self.z, self.yaw, self.age, self.tracking)

    def submit(self, generation, mode, v, w):
        if generation != self.generation or mode != 'manual':
            return False
        self.sent.append((v, w))
        self.clock.t += .1
        if self.responsive:
            heading = self.yaw
            self.x += v * .1 * self.scale * math.sin(heading) + self.lateral * math.cos(heading)
            self.z += v * .1 * self.scale * math.cos(heading) - self.lateral * math.sin(heading)
            self.yaw += w * .1 * self.scale
        if len(self.sent) in self.hooks:
            self.hooks[len(self.sent)]()
        return True

    def stop(self, reason):
        self.stops.append(reason)
        self.generation += 1
        self.armed_mode = None
        self.z += self.coast
        self.runner.halt(reason)

    async def finish(self, request, generation=None, timeout=5.):
        self.runner.start(request, self.generation if generation is None else generation, 'p1')
        for _ in range(int(timeout * 1000)):
            if not self.runner.active:
                return self.runner.result
            await asyncio.sleep(.001)
        raise AssertionError('move did not finish')


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_forward_stops_at_the_measured_distance_and_reports_it(self):
        rig = Rig(self)
        result = await rig.finish(MoveRequest('forward', 20, 'cm'))
        self.assertEqual((result['status'], result['reason']), ('completed', 'move_complete'))
        self.assertEqual(rig.stops, ['move_complete'])
        self.assertTrue(all(cmd == (.1, 0.) for cmd in rig.sent))
        self.assertGreaterEqual(result['achieved'], .2 - 1e-9)
        self.assertLess(result['achieved'], .2 + .011)
        self.assertEqual(result['quantity'], 'distance')
        self.assertIn('Moved forward 0.20 m of the requested 20 cm', result['text'])

    async def test_a_turn_uses_measured_yaw_and_the_requested_side(self):
        rig = Rig(self)
        result = await rig.finish(MoveRequest('right', 30, 'deg'))
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(all(cmd == (0., -.4) for cmd in rig.sent))
        self.assertGreaterEqual(result['achieved'], 30 - 1e-6)
        self.assertLess(result['achieved'], 30 + 2.5)
        self.assertIn('Turned right', result['text'])

    async def test_timed_pulses_without_measured_progress_never_count_as_distance(self):
        rig = Rig(self)
        rig.responsive = False  # motors commanded, pose unchanged
        result = await rig.finish(MoveRequest('forward', 20, 'cm'))
        self.assertEqual((result['status'], result['reason']), ('failed', 'move_no_progress'))
        self.assertEqual(result['achieved'], 0.)
        self.assertIn('0.00 m of the requested 20 cm', result['text'])

    async def test_a_wrong_way_turn_stops_early(self):
        rig = Rig(self)
        rig.scale = -1.  # yaw sign misconfigured: commanded left, the rover turns right
        result = await rig.finish(MoveRequest('left', 90, 'deg'))
        self.assertEqual((result['status'], result['reason']), ('failed', 'move_diverging'))
        self.assertLess(result['achieved'], 0)
        self.assertGreater(result['achieved'], -15)
        self.assertIn('did not move in the requested direction', result['text'])

    async def test_sideways_drift_or_reversing_during_a_forward_move_stops_it(self):
        rig = Rig(self)
        rig.lateral = .02
        self.assertEqual((await rig.finish(MoveRequest('forward', 50, 'cm')))['reason'], 'move_diverging')
        rig = Rig(self)
        rig.scale = -1.
        self.assertEqual((await rig.finish(MoveRequest('forward', 50, 'cm')))['reason'], 'move_diverging')

    async def test_stale_or_lost_tracking_ends_the_move_with_the_last_measurement(self):
        for change, reason in ((dict(age=.3), 'pose_stale'), (dict(tracking='limited'), 'tracking_lost')):
            with self.subTest(reason=reason):
                rig = Rig(self)
                rig.hooks[3] = lambda: [setattr(rig, key, value) for key, value in change.items()]
                result = await rig.finish(MoveRequest('forward', 50, 'cm'))
                self.assertEqual((result['status'], result['reason']), ('failed', reason))
                self.assertAlmostEqual(result['achieved'], .02, places=6)  # the last fresh, tracked pose
                self.assertEqual(rig.stops, [reason])

    async def test_timeout_bounds_a_slow_move(self):
        rig = Rig(self)
        rig.scale = .3  # progress continues but far slower than commanded
        result = await rig.finish(MoveRequest('forward', 50, 'cm'))
        self.assertEqual((result['status'], result['reason']), ('failed', 'move_timeout'))
        self.assertLess(result['achieved'], .5)

    async def test_an_external_stop_ends_it_once_and_nothing_more_is_sent(self):
        rig = Rig(self)
        rig.hooks[2] = lambda: rig.stop('operator_stop')
        result = await rig.finish(MoveRequest('forward', 50, 'cm'))
        self.assertEqual(len(rig.sent), 2)
        self.assertEqual(rig.stops, ['operator_stop'])
        self.assertEqual((result['status'], result['reason']), ('stopped', 'operator_stop'))
        self.assertIn('Stopped after 0.02 m of the requested 50 cm', result['text'])
        self.assertIn('you pressed Stop', result['text'])

    async def test_disarm_or_a_refused_command_ends_the_run(self):
        rig = Rig(self)
        rig.hooks[1] = lambda: setattr(rig, 'armed_mode', 'navigate')
        self.assertEqual((await rig.finish(MoveRequest('forward', 50, 'cm')))['reason'], 'disarmed')
        self.assertEqual(len(rig.sent), 1)
        rig = Rig(self)
        # The arm this move was confirmed under already ended without a stop reaching the runner.
        result = await rig.finish(MoveRequest('forward', 20, 'cm'), generation=rig.generation - 1)
        self.assertEqual((result['reason'], rig.sent), ('command_stale', []))

    async def test_settling_reports_coasting_after_the_stop(self):
        rig = Rig(self, settle_s=.05)
        rig.coast = .03
        result = await rig.finish(MoveRequest('forward', 20, 'cm'))
        self.assertGreater(result['achieved'], .225)
        self.assertIn('0.23 m of the requested 20 cm', result['text'])

    async def test_one_move_at_a_time_and_its_status_while_running(self):
        rig = Rig(self)
        state = rig.runner.start(MoveRequest('forward', 50, 'cm'), rig.generation, 'p1')
        self.assertEqual(state['status'], 'running')
        with self.assertRaises(RuntimeError):
            rig.runner.start(MoveRequest('left', 30, 'deg'), rig.generation, 'p2')
        rig.stop('operator_stop')
        while rig.runner.active:
            await asyncio.sleep(.001)

    async def test_no_fresh_pose_at_start_is_refused_before_any_command(self):
        rig = Rig(self)
        rig.gone = True
        result = await rig.finish(MoveRequest('forward', 20, 'cm'))
        self.assertEqual((result['reason'], result['achieved']), ('pose_stale', None))
        self.assertEqual(rig.sent, [])
        self.assertIn('before any measured movement', result['text'])


SESSION = dict(session_id='rehearsal', map_epoch=1)


def kinematic(car, source, scale=1.):
    """FakeCar whose phone pose follows each sent command for one 50 ms pump tick (scale<0: wrong way)."""
    original = car.send

    def send(command):
        original(command)
        dt = .05 * scale
        x, y, z = source.position
        source.position = (x + command.v_mps * dt * math.sin(source.yaw), y,
                           z + command.v_mps * dt * math.cos(source.yaw))
        source.yaw += command.yaw_rate_rps * dt
    car.send = send


class MoveAppTests(unittest.TestCase):
    """The real app with FakeCar, a synthetic phone and NO rover calibration: a move needs no map."""

    def propose(self, client, direction='forward', amount=20, unit='cm'):
        action = dict(id=uuid4().hex[:12], name='propose_move', args=dict(direction=direction, amount=amount,
                                                                         unit=unit))
        return client.post('/nav/propose', json=dict(SESSION, action=action))

    def confirm(self, client, proposal):
        return client.post('/nav/confirm', json=dict(SESSION, proposal_id=proposal['proposal_id']))

    def settled(self, client):
        wait_for(lambda: client.get('/nav/move').json()['move']['status'] != 'running', 10.)
        return client.get('/nav/move').json()['move']

    def moving(self, car):
        return [call for call in car.calls if call[0] == 'send' and (call[1] or call[2])]

    def test_proposing_moves_nothing_and_explains_the_limits_and_the_arm(self):
        with scene(calibration=None) as (client, car, source):
            proposal = self.propose(client).json()
            self.assertEqual((proposal['status'], proposal['kind']), ('ready', 'move'), proposal)
            self.assertEqual(proposal['move']['label'], 'forward 20 cm (0.20 m)')
            self.assertEqual(proposal['execution']['reason'], 'move_arm_required')
            self.assertFalse(proposal['hardware_verified'])
            far = self.propose(client, amount=100, unit='in').json()
            self.assertEqual((far['status'], far['reason'], far['proposal_id']),
                             ('unavailable', 'move_out_of_range', None))
            self.assertIn('5 to 50 centimeters', far['message'])
            self.assertEqual(self.moving(car), [])
            self.assertEqual(self.confirm(client, proposal).status_code, 409)  # not armed: consumed anyway
            self.assertEqual(self.confirm(client, proposal).status_code, 404)
            arm(client, 'navigate')  # armed, but not in manual mode
            late = self.propose(client).json()
            answer = self.confirm(client, late)
            self.assertEqual((answer.status_code, answer.json()['detail']), (409, 'move_arm_required'))
            client.post('/stop')
            voided = self.propose(client).json()
            arm(client, 'manual')
            client.post('/stop')  # any stop voids pending confirmations
            self.assertEqual(self.confirm(client, voided).status_code, 404)
            self.assertEqual(self.moving(car), [])

    def test_a_confirmed_move_runs_once_measured_by_pose_and_disarms(self):
        with scene(calibration=None, follower=FollowerConfig(min_mps=.15)) as (client, car, source):
            kinematic(car, source)
            proposal = self.propose(client).json()
            arm(client, 'manual')
            started = self.confirm(client, proposal)
            self.assertEqual(started.status_code, 200, started.text)
            self.assertEqual(started.json()['move']['status'], 'running')
            second = self.propose(client, 'left', 30, 'deg').json()
            busy = self.confirm(client, second)
            self.assertEqual((busy.status_code, busy.json()['detail']), (409, 'move_in_progress'))
            result = self.settled(client)
            self.assertEqual((result['status'], result['reason']), ('completed', 'move_complete'), result)
            self.assertGreaterEqual(result['achieved'], .19)
            self.assertLess(result['achieved'], .3)
            self.assertFalse(client.app.state.armed)
            self.assertEqual(client.app.state.stop_reason, 'move_complete')
            self.assertTrue(all(call[1:] == (.15, 0.) for call in self.moving(car)))
            self.assertEqual(car.calls[-1], ('zero',))
            self.assertEqual(self.confirm(client, proposal).status_code, 404)  # never replayed
            sent = len(self.moving(car))
            time.sleep(.3)
            self.assertEqual(len(self.moving(car)), sent)

    def test_a_turn_and_an_operator_stop_report_the_measured_partial_result(self):
        with scene(calibration=None) as (client, car, source):
            kinematic(car, source)
            arm(client, 'manual')
            self.assertEqual(self.confirm(client, self.propose(client, 'left', 30, 'deg').json()).status_code, 200)
            result = self.settled(client)
            self.assertEqual(result['status'], 'completed', result)
            self.assertGreaterEqual(result['achieved'], 29.9)
            self.assertTrue(all(call[1] == 0. and call[2] > 0 for call in self.moving(car)))
            arm(client, 'manual')
            self.assertEqual(self.confirm(client, self.propose(client, amount=50).json()).status_code, 200)
            wait_for(lambda: len(self.moving(car)) > 20)
            client.post('/stop')
            result = self.settled(client)
            self.assertEqual((result['status'], result['reason']), ('stopped', 'operator_stop'))
            self.assertLess(result['achieved'], .5)
            self.assertIn('you pressed Stop', result['text'])

    def test_a_wrong_way_turn_is_stopped(self):
        with scene(calibration=None) as (client, car, source):
            kinematic(car, source, scale=-1.)
            arm(client, 'manual')
            self.confirm(client, self.propose(client, 'right', 90, 'deg').json())
            result = self.settled(client)
            self.assertEqual((result['status'], result['reason']), ('failed', 'move_diverging'), result)
            self.assertGreater(result['achieved'], -20)


class SpeakTests(unittest.TestCase):
    def test_only_a_finished_move_is_spoken_once_in_the_backends_own_words(self):
        spoken = []

        async def speak(text):
            spoken.append(text)
            return dict(status='ready')

        class Runner:
            result = None

        app = FastAPI()
        register_move_routes(app, lambda: Runner, speak)
        client = TestClient(app)
        self.assertEqual(client.get('/nav/move').json(), dict(version=1, move=None))
        self.assertEqual(client.post('/nav/move/speak', json=dict(move_id='m1')).status_code, 404)
        Runner.result = dict(move_id='m1', status='running', text=None)
        self.assertEqual(client.post('/nav/move/speak', json=dict(move_id='m1')).status_code, 409)
        Runner.result = dict(move_id='m1', status='completed', text='Moved forward 0.20 m.')
        self.assertEqual(client.post('/nav/move/speak', json=dict(move_id='m1')).json()['speech'],
                         dict(status='ready'))
        self.assertEqual(client.post('/nav/move/speak', json=dict(move_id='m1')).status_code, 409)
        self.assertEqual(spoken, ['Moved forward 0.20 m.'])
        silent = FastAPI()
        register_move_routes(silent, lambda: Runner, None)
        self.assertEqual(TestClient(silent).post('/nav/move/speak', json=dict(move_id='m1')).status_code, 503)


if __name__ == '__main__':
    unittest.main()
