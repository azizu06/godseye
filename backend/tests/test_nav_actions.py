"""Voice navigation proposals: strict action seam, validation against the live map, single-use confirm.

Fake hardware only: FakeCar, a synthetic phone scene and TEST calibration describing no real rover.
"""
import json
import math
import time
import unittest
from uuid import uuid4

import numpy as np

from backend.nav_actions import APPROACH_MAX_M, PROPOSAL_TTL_S, approach_point, validate_nav_action
from backend.navigation import Grid, PlannerConfig
from tools.car_rehearsal import arm, scene, wait_for

UNKNOWN, FREE, OCCUPIED = 0, 1, 2
SESSION = dict(session_id='rehearsal', map_epoch=1)


class ActionSeamTests(unittest.TestCase):
    def test_accepts_the_reserved_actions_with_exact_args(self):
        for entry in (
                dict(id='a1', name='propose_navigation', args={'target': 'object', 'object_id': 'x1',
                                                               'class': 'backpack'}),
                dict(id='a3', name='propose_navigation', args=dict(target='point', x=1.5, z=-2)),
                dict(id='a4', name='propose_exploration', args={}),
                dict(id='a5', name='stop_navigation', args={}),
                dict(id='a6', name='propose_move', args=dict(direction='forward', amount=20., unit='cm')),
                dict(id='a7', name='propose_move', args=dict(direction='right', amount=30., unit='deg'))):
            with self.subTest(entry=entry):
                self.assertEqual(validate_nav_action(entry), entry)

    def test_drops_anything_else_without_repairing_it(self):
        for entry in (
                None, [], 'propose_navigation',
                dict(id='a', name='arm', args={}),
                dict(id='a', name='set_view', args={}),
                dict(id='a', name='propose_exploration'),
                dict(id='a', name='propose_exploration', args={'speed': 1}),
                dict(id='a', name='stop_navigation', args=None),
                dict(id='', name='stop_navigation', args={}),
                dict(id='x' * 65, name='stop_navigation', args={}),
                dict(id=3, name='stop_navigation', args={}),
                dict(id='a', name='stop_navigation', args={}, extra=True),
                dict(id='a', name='propose_navigation', args=dict(target='object')),
                dict(id='a', name='propose_navigation', args={'target': 'object', 'class': 'bag'}),
                dict(id='a', name='propose_navigation', args={'target': 'object', 'ref': 'o1'}),  # unresolved
                dict(id='a', name='propose_navigation', args={'target': 'object', 'object_id': '', 'class': 'bag'}),
                dict(id='a', name='propose_navigation', args={'target': 'object', 'object_id': 'x',
                                                              'class': 'b' * 65}),
                dict(id='a', name='propose_navigation', args={'target': 'object', 'object_id': 'x', 'class': 'bag',
                                                              'x': 1., 'z': 1.}),
                dict(id='a', name='propose_navigation', args=dict(target='point', x=1.)),
                dict(id='a', name='propose_navigation', args=dict(target='point', x='1', z=1.)),
                dict(id='a', name='propose_navigation', args=dict(target='point', x=float('nan'), z=1.)),
                dict(id='a', name='propose_navigation', args=dict(target='point', x=51., z=0.)),
                dict(id='a', name='propose_navigation', args={'target': 'point', 'x': 1., 'z': 1., 'class': 'bag'}),
                dict(id='a', name='propose_navigation', args=dict(target='point', x=True, z=1.)),
                dict(id='a', name='propose_navigation', args={'target': 'object', 'object_id': 'x', 'class': 'bag',
                                                              'speed_mps': 1.}),
                dict(id='a', name='propose_navigation', args=dict(target='home')),
                dict(id='a', name='propose_move', args=dict(direction='back', amount=20., unit='cm')),
                dict(id='a', name='propose_move', args=dict(direction='forward', amount=20., unit='deg')),
                dict(id='a', name='propose_move', args=dict(direction='left', amount=20., unit='in')),
                dict(id='a', name='propose_move', args=dict(direction='forward', amount=0., unit='cm')),
                dict(id='a', name='propose_move', args=dict(direction='forward', amount=True, unit='cm')),
                dict(id='a', name='propose_move', args=dict(direction='forward', amount=20.)),
                dict(id='a', name='propose_move', args=dict(direction='forward', amount=20., unit='cm', v_mps=.2))):
            with self.subTest(entry=entry):
                self.assertIsNone(validate_nav_action(entry))


class ApproachPointTests(unittest.TestCase):
    config = PlannerConfig(robot_radius_m=.2, margin_m=0., unknown_traversable=False, footprint_clearance=True,
                           snap_radius_m=0., start_snap_radius_m=0.)

    def test_stops_short_of_an_occupied_object_with_its_footprint_clear(self):
        cells = np.full((60, 60), FREE, np.uint8)
        cells[38:42, 28:32] = OCCUPIED  # a 20 cm object centered at (1.5, 2.0)
        grid = Grid.from_array(cells, origin=(0., 0.), cell_m=.05)
        point = approach_point(grid, self.config, (1.5, .5), (1.5, 2.), min_m=.2)
        self.assertIsNotNone(point)
        distance = math.dist(point, (1.5, 2.))
        # Nearest clear floor on the rover's side: the object's near edge (z 1.9) plus the 0.2 m footprint.
        self.assertTrue(.3 <= distance <= .4, distance)
        self.assertLess(point[1], 2.)
        # The whole 0.2 m footprint disc stays off the object's cells (x 1.4-1.6, z 1.9-2.1).
        gap = math.hypot(max(1.4 - point[0], 0, point[0] - 1.6), max(1.9 - point[1], 0, point[1] - 2.1))
        self.assertGreaterEqual(gap, .2)

    def test_never_targets_a_center_that_reads_as_floor(self):
        grid = Grid.from_array(np.full((60, 60), FREE, np.uint8), origin=(0., 0.), cell_m=.05)
        point = approach_point(grid, self.config, (1.5, .5), (1.5, 2.), min_m=.3)
        self.assertGreaterEqual(math.dist(point, (1.5, 2.)), .3)

    def test_nothing_reachable_and_clear_within_a_meter_is_none(self):
        cells = np.full((60, 60), FREE, np.uint8)
        cells[30, :] = OCCUPIED  # a wall at z 1.5; the object is 1.3 m behind it
        grid = Grid.from_array(cells, origin=(0., 0.), cell_m=.05)
        self.assertIsNone(approach_point(grid, self.config, (1.5, .5), (1.5, 2.8), min_m=.2))
        unknown = np.full((60, 60), FREE, np.uint8)
        unknown[35:, :] = UNKNOWN
        grid = Grid.from_array(unknown, origin=(0., 0.), cell_m=.05)
        self.assertIsNone(approach_point(grid, self.config, (1.5, .5), (1.5, 2.9), min_m=.2))


class ProposalTests(unittest.TestCase):
    """The real app, a synthetic surveyed floor (x about -2..2, z about -1..1.3, no obstacles in view),
    TEST calibration and FakeCar. Objects are stored directly, as the detector would."""

    def put(self, client, name, x, z, state='present', label=None):
        object_id = str(uuid4())
        client.portal.call(self.store, client.app.state.db, object_id, name, x, z, state, label)
        return object_id

    @staticmethod
    def store(db, object_id, name, x, z, state, label):  # on the app's own thread, which owns the connection
        db.execute('INSERT INTO objects(id,session_id,map_epoch,class,position_json,identity_confidence,'
                   'first_seen,last_seen,observations,state) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (object_id, 'rehearsal', 1, name, json.dumps([x, -1., z]), .9, time.time(), time.time(), 3,
                    state))
        if label is not None:
            db.execute('INSERT INTO object_labels(object_id,label,status,reason) VALUES(?,?,?,?)',
                       (object_id, label, 'labeled', None))
        db.commit()

    def propose(self, client, name='propose_navigation', args=None, session=SESSION):
        action = dict(id=uuid4().hex[:12], name=name, args=args if args is not None else self.target())
        return client.post('/nav/propose', json=dict(session, action=action))

    def confirm(self, client, proposal, session=SESSION):
        return client.post('/nav/confirm', json=dict(session, proposal_id=proposal['proposal_id']))

    def target(self, object_id=None, name='backpack'):
        # voice.resolve_actions has already turned the model's ref into the stored id.
        return {'target': 'object', 'object_id': object_id or self.backpack, 'class': name}

    def moved(self, car):
        return [call for call in car.calls if call[0] == 'send' and (call[1] or call[2])]

    def test_an_object_destination_is_a_rover_clear_approach_and_proposing_moves_nothing(self):
        with scene() as (client, car, source):
            self.backpack = self.put(client, 'backpack', .8, .9)
            before = list(car.calls)
            proposal = self.propose(client).json()
            self.assertEqual(proposal['status'], 'ready', proposal)
            self.assertEqual(proposal['kind'], 'destination')
            self.assertEqual(proposal['target']['class'], 'backpack')
            inflation = client.app.state.map_snapshot().inflation_m
            distance = math.dist(proposal['destination'], (.8, .9))
            self.assertTrue(inflation <= distance <= APPROACH_MAX_M, distance)
            self.assertEqual(proposal['points'][-1], proposal['destination'])
            self.assertGreater(proposal['length_m'], 0)
            self.assertEqual(proposal['execution']['reason'], 'arm_required')
            self.assertEqual(proposal['execution']['autonomy_blockers'], ['logging_adapter_only'])
            self.assertIn('cannot drive', proposal['execution']['autonomy_message'])
            self.assertFalse(proposal['hardware_verified'])
            self.assertEqual(proposal['expires_in_s'], PROPOSAL_TTL_S)
            # Prompt-only output: no arm, mode change, run, path or drive command.
            health = client.get('/health').json()
            self.assertEqual((health['armed'], health['mode']), (False, 'manual'))
            self.assertFalse(client.app.state.nav.active)
            self.assertEqual(client.app.state.nav.path, [])
            self.assertEqual(car.calls, before)

    def test_confirm_needs_a_deliberate_arm_and_the_proposal_is_single_use(self):
        with scene() as (client, car, source):
            self.backpack = self.put(client, 'backpack', .8, .9)
            proposal = self.propose(client).json()
            answer = self.confirm(client, proposal)
            self.assertEqual((answer.status_code, answer.json()['detail']), (409, 'arm_required'))
            self.assertEqual(self.confirm(client, proposal).status_code, 404)  # consumed: never replayed
            self.assertFalse(client.app.state.armed)
            self.assertEqual(self.moved(car), [])

    def test_confirmed_destination_follows_the_existing_goal_path_once(self):
        with scene() as (client, car, source):
            self.backpack = self.put(client, 'backpack', .8, .9)
            proposal = self.propose(client).json()
            # The dashboard's existing hand-off: select navigate mode, arm deliberately, then confirm.
            arm(client, 'navigate')
            answer = self.confirm(client, proposal)
            self.assertEqual(answer.status_code, 200, answer.text)
            body = answer.json()
            self.assertEqual(body['goal'], proposal['destination'])
            self.assertEqual(body['proposal_id'], proposal['proposal_id'])
            self.assertEqual(client.app.state.nav.kind, 'goal')
            self.assertEqual(list(client.app.state.nav.goal), proposal['destination'])
            wait_for(lambda: self.moved(car))
            for _, v, w in self.moved(car):
                self.assertTrue(0 <= v <= .15 and abs(w) <= .5)
            retry = self.confirm(client, proposal)
            self.assertEqual(retry.status_code, 404)
            client.post('/stop')
            self.assertFalse(client.app.state.nav.active)

    def test_stop_session_reset_and_health_loss_void_pending_proposals_but_a_mode_switch_does_not(self):
        with scene() as (client, car, source):
            self.backpack = self.put(client, 'backpack', .8, .9)
            kept = self.propose(client).json()
            client.post('/mode', json=dict(mode='navigate'))  # part of the dashboard hand-off
            client.post('/arm')
            self.assertEqual(self.confirm(client, kept).status_code, 200)
            for trigger in (lambda: client.post('/stop'),
                            lambda: setattr(car, 'state', 'stale')):
                car.state = 'ok'
                wait_for(lambda: client.get('/health').json()['car'] == 'ok')
                arm(client, 'navigate')
                voided = self.propose(client).json()
                self.assertEqual(voided['status'], 'ready', voided)
                trigger()
                wait_for(lambda: not client.app.state.armed)
                car.state = 'ok'
                arm(client, 'navigate')
                self.assertEqual(self.confirm(client, voided).status_code, 404)
                self.assertFalse(client.app.state.nav.active)
            client.post('/stop')
            voided = self.propose(client).json()
            new = client.post('/session').json()
            self.assertEqual(self.confirm(client, voided).status_code, 404)
            stale_map = self.propose(client)  # the old map is no longer the active one
            self.assertEqual(stale_map.status_code, 409)
            self.assertNotEqual(new['session_id'], 'rehearsal')

    def test_stale_health_is_explained_and_blocks_confirmation(self):
        with scene() as (client, car, source):
            self.backpack = self.put(client, 'backpack', .8, .9)
            car.state = 'stale'
            wait_for(lambda: client.get('/health').json()['car'] == 'stale')
            proposal = self.propose(client).json()
            self.assertEqual(proposal['status'], 'ready')
            self.assertEqual(proposal['execution']['reason'], 'car_stale')
            self.assertIn('not physically validated', proposal['execution']['message'])
            answer = self.confirm(client, proposal)
            self.assertEqual((answer.status_code, answer.json()['detail']), (409, 'car_stale'))
            self.assertEqual(self.moved(car), [])

    def test_missing_calibration_makes_every_proposal_unavailable(self):
        with scene(calibration=None) as (client, car, source):
            self.backpack = self.put(client, 'backpack', .8, .9)
            for name, args in (('propose_navigation', None), ('propose_exploration', {})):
                proposal = self.propose(client, name, args).json()
                self.assertEqual((proposal['status'], proposal['reason']), ('unavailable', 'calibration_missing'))
                self.assertIsNone(proposal['proposal_id'])
                self.assertIn('calibration', proposal['message'])

    def test_missing_unconfirmed_and_unreachable_targets_explain_instead_of_guessing(self):
        with scene() as (client, car, source):
            self.backpack = self.put(client, 'backpack', .8, .9)
            chair = self.put(client, 'chair', 0., 4.)  # beyond mapped floor
            bottle = self.put(client, 'bottle', .5, .5, state='not_found_on_rescan')
            for args, reason in ((self.target('no-such-object'), 'target_not_found'),
                                 (self.target(name='chair'), 'target_not_found'),  # id and class disagree
                                 (self.target(bottle, 'bottle'), 'target_not_confirmed'),
                                 (self.target(chair, 'chair'), 'no_clear_approach'),
                                 (dict(target='point', x=0., z=4.), 'out_of_bounds')):
                with self.subTest(reason=reason):
                    proposal = self.propose(client, args=args).json()
                    self.assertEqual((proposal['status'], proposal['reason']), ('unavailable', reason))
                    self.assertIsNone(proposal['proposal_id'])
                    if args['target'] == 'object':
                        self.assertIn('map', proposal['alternative'])  # pick the floor instead
            invalid = self.propose(client, args=dict(self.target(), speed_mps=1.))
            self.assertEqual(invalid.status_code, 422)

    def test_a_moved_target_or_expired_proposal_is_refused_at_confirm(self):
        with scene() as (client, car, source):
            object_id = self.backpack = self.put(client, 'backpack', .8, .9)
            arm(client, 'navigate')
            proposal = self.propose(client).json()
            client.portal.call(lambda: client.app.state.db.execute(
                'UPDATE objects SET position_json=? WHERE id=?', (json.dumps([.8, -1., .3]), object_id)))
            answer = self.confirm(client, proposal)
            self.assertEqual((answer.status_code, answer.json()['detail']), (409, 'target_moved'))
            proposal = self.propose(client, args=dict(target='point', x=0., z=.5)).json()
            clock = client.app.state.nav_proposals.clock
            client.app.state.nav_proposals.clock = lambda: clock() + PROPOSAL_TTL_S + 1
            answer = self.confirm(client, proposal)
            self.assertEqual((answer.status_code, answer.json()['detail']), (409, 'proposal_expired'))
            self.assertFalse(client.app.state.nav.active)
            self.assertEqual(self.moved(car), [])

    def test_confirmed_exploration_only_selects_explore_and_still_needs_an_arm(self):
        with scene() as (client, car, source):
            arm(client, 'navigate')
            proposal = self.propose(client, 'propose_exploration', {}).json()
            self.assertEqual((proposal['status'], proposal['kind']), ('ready', 'exploration'), proposal)
            self.assertEqual(len(proposal['frontier']), 2)
            answer = self.confirm(client, proposal)
            self.assertEqual(answer.status_code, 200, answer.text)
            body = answer.json()
            self.assertEqual((body['mode'], body['armed'], body['next']), ('explore', False, 'arm'))
            time.sleep(.2)
            self.assertFalse(client.app.state.nav.active)
            self.assertEqual(self.confirm(client, proposal).status_code, 404)
            client.post('/arm')  # the deliberate arm starts the existing frontier planner
            wait_for(lambda: client.app.state.nav.kind == 'explore')
            client.post('/stop')

    def test_a_stop_suggestion_is_never_executed_by_the_backend(self):
        with scene() as (client, car, source):
            arm(client, 'navigate')
            proposal = self.propose(client, 'stop_navigation', {}).json()
            self.assertEqual((proposal['kind'], proposal['status'], proposal['proposal_id']), ('stop', 'ready', None))
            self.assertTrue(client.get('/health').json()['armed'])  # the human presses Stop
            client.post('/stop')

    def test_a_spoken_stop_stops_a_running_route_at_once_and_voids_every_suggestion(self):
        from functools import partial
        from unittest.mock import patch
        from backend.app import create_app
        from backend.tests.test_voice import CLIP, WEBM, FakeTranscriber, providers
        voice = providers(FakeTranscriber('Stop, stop!'))
        with patch('tools.car_rehearsal.create_app', partial(create_app, voice_providers=voice)), \
                scene() as (client, car, source):
            self.backpack = self.put(client, 'backpack', .8, .9)
            arm(client, 'navigate')
            self.assertEqual(self.confirm(client, self.propose(client).json()).status_code, 200)
            wait_for(lambda: self.moved(car))
            pending = self.propose(client, args=dict(target='point', x=0., z=.5)).json()
            self.assertEqual(pending['status'], 'ready', pending)
            # "go" is only handed to the dashboard's card: the backend confirms and consumes nothing.
            voice.transcriber.text = 'go'
            self.assertEqual(client.post('/voice/ask', content=CLIP, headers=WEBM).json()['command'], 'confirm')
            self.assertEqual(client.app.state.nav.kind, 'goal')
            voice.transcriber.text = 'Stop, stop!'
            result = client.post('/voice/ask', content=CLIP, headers=WEBM).json()
            self.assertEqual((result['command'], result['stopped']), ('stop', True))
            health = client.get('/health').json()
            self.assertEqual((health['armed'], health['stop_reason']), (False, 'operator_stop'))
            self.assertFalse(client.app.state.nav.active)
            kinds = [call[0] for call in car.calls]
            self.assertGreater(len(kinds) - kinds[::-1].index('zero'), len(kinds) - kinds[::-1].index('send'))
            self.assertEqual(self.confirm(client, pending).status_code, 404)  # voided like any operator Stop
            self.assertEqual(voice.answerer.calls, [])


class RelayAuthorizationTests(unittest.TestCase):
    def test_confirming_needs_the_rover_pairing_key_like_every_motion_route(self):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        from backend.rover_relay import RelayCar
        from backend.tests.test_actuation import fixture
        key = 'ONLY_A_TEST_KEY_NOT_A_REAL_ROVER_KEY'
        with TestClient(create_app(db_path=':memory:', car=RelayCar(key, fixture(forward=None)))) as client:
            body = dict(SESSION, proposal_id='p')
            self.assertEqual(client.post('/nav/confirm', json=body).status_code, 401)
            self.assertEqual(client.post('/nav/confirm', json=body,
                                         headers={'Authorization': 'Bearer ' + key}).status_code, 404)
            # Proposing and cancelling cannot move anything, so they stay open like map reads and Stop.
            self.assertEqual(client.post('/nav/cancel', json=dict(proposal_id='p')).status_code, 200)
            self.assertEqual(client.post('/nav/propose', json=dict(SESSION, action=dict(
                id='a', name='stop_navigation', args={}))).status_code, 409)  # no active map, not 401

    def test_the_uncalibrated_prototype_profile_is_named_on_every_suggestion(self):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        from backend.prototype import PrototypeActuation
        from backend.rover_relay import RelayCar
        key = 'ONLY_A_TEST_KEY_NOT_A_REAL_ROVER_KEY'
        with TestClient(create_app(db_path=':memory:', car=RelayCar(key, PrototypeActuation()))) as client:
            session = client.post('/session').json()
            proposal = client.post('/nav/propose', json=dict(
                session_id=session['session_id'], map_epoch=1,
                action=dict(id='a', name='stop_navigation', args={}))).json()
            self.assertTrue(any('Uncalibrated prototype' in w for w in proposal['execution']['autonomy_warnings']))
            self.assertIn('rover_relay_disconnected', proposal['execution']['autonomy_blockers'])


if __name__ == '__main__':
    unittest.main()
