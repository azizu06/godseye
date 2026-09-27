"""Real relay state/HTTP/WebSocket paths, synthetic measurements, no hardware."""
import asyncio
import json
import threading
import time
import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.motion import DriveCommand, DriveStop
from backend.rover_relay import RelayCar
from backend.tests.test_actuation import fixture
from backend.calibration import RoverCalibration
from tools.car_rehearsal import EmptyDetector, PhoneScene, TEST_CALIBRATION, wait_for

KEY = 'ONLY_A_TEST_KEY_NOT_A_REAL_ROVER_KEY'
SESSION = '0123456789abcdef0123456789abcdef'


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 10.
        self.car = RelayCar(KEY, fixture(), clock=lambda: self.now)
        self.car.identity = lambda: ('capture', 1)
        self.losses = []
        self.car.on_loss = self.losses.append
        self.car.attach()
        self.counter = 0
        self.feedback()

    def feedback(self, **changes):
        self.counter += 1
        status = dict(version=1, type='status', seq=self.counter, session_id='capture',
                      map_epoch=1, permit=f'{self.counter:016X}', uno_age_ms=120., enabled=True)
        status.update(changes)
        self.car.receive(status)
        return status

    async def settle(self):
        for _ in range(4):
            await asyncio.sleep(0)

    async def arm(self):
        self.car.zero(DriveStop(None, 0, 0))
        pending = asyncio.create_task(self.car.prepare(SESSION))
        await self.settle()
        stop = self.car.next_message()
        self.assertEqual(stop['type'], 'stop')
        self.car.receive(dict(version=1, type='ack', id='Z' + stop['id']))
        await self.settle()
        self.assertIsNone(self.car.next_message(), 'arm must wait for a post-Stop permit')
        self.feedback()
        await self.settle()
        arm = self.car.next_message()
        self.assertEqual(arm['type'], 'arm')
        self.assertEqual(arm['session'], SESSION.upper())
        self.car.receive(dict(version=1, type='ack', id='A' + SESSION.upper()))
        await self.settle()
        self.assertFalse(pending.done(), 'arm must wait for a post-arm permit')
        self.feedback()
        await pending

    def drive(self, seq=1, **changes):
        fields = dict(session_id=SESSION, seq=seq, v_mps=.1, yaw_rate_rps=0.,
                      issued_at_ms=round(self.now * 1000), valid_for_ms=250)
        fields.update(changes)
        return DriveCommand(**fields)

    async def test_arm_barrier_then_calibrated_latest_only_timed_command(self):
        await self.arm()
        for seq in range(1, 21):
            self.car.send(self.drive(seq))
        packet = self.car.next_message()
        self.assertEqual(packet, dict(version=1, type='command', session=SESSION.upper(),
                                     seq=20, permit=self.car.status.permit,
                                     direction=3, power=40, lease_ms=200))
        self.assertIsNone(self.car.next_message())
        with self.assertRaises(ValueError):
            self.car.send(self.drive(20))

    async def test_zero_keeps_session_stop_retires_it_and_drops_queue(self):
        await self.arm()
        self.car.zero(self.drive(v_mps=0.))
        packet = self.car.next_message()
        self.assertEqual((packet['direction'], packet['power']), (0, 0))
        self.car.send(self.drive(2))
        self.car.zero(DriveStop(SESSION, 3, round(self.now * 1000)))
        self.assertEqual(self.car.next_message()['type'], 'stop')
        self.assertIsNone(self.car.next_message())
        with self.assertRaises(ValueError):
            self.car.send(self.drive(4))

    async def test_dispatch_age_rechecked_after_backpressure_and_never_renewed(self):
        await self.arm()
        self.car.send(self.drive())
        self.now += .151
        self.feedback()
        self.assertEqual(self.car.next_message()['type'], 'stop')
        self.assertEqual(self.losses, ['rover_dispatch_stale'])
        self.assertIsNone(self.car.armed_session)
        with self.assertRaises(ValueError):
            self.car.send(self.drive(2, issued_at_ms=10000))

    async def test_stale_feedback_wrong_capture_and_unmeasured_rates_refused(self):
        await self.arm()
        with self.assertRaises(ValueError): self.car.send(self.drive(yaw_rate_rps=.2))
        with self.assertRaises(ValueError): self.car.send(self.drive(v_mps=.02))
        with self.assertRaises(ValueError): self.car.send(self.drive(issued_at_ms=99999999))
        self.now += .201
        self.assertEqual(self.car.health(), 'stale')
        with self.assertRaises(ValueError): self.car.send(self.drive())
        self.feedback(map_epoch=2)
        self.assertIn('rover_capture_mismatch', self.car.blockers())
        self.car.detach()
        self.assertEqual(self.car.health(), 'down')
        self.car.attach()
        self.feedback()
        with self.assertRaises(ValueError): self.car.send(self.drive())

    async def test_stop_interrupts_arm_and_old_ack_cannot_reopen(self):
        pending = asyncio.create_task(self.car.prepare(SESSION))
        await self.settle()
        old = self.car.next_message()
        self.car.zero(DriveStop(None, 0, 0))
        self.car.receive(dict(version=1, type='ack', id='Z' + old['id']))
        self.feedback()
        with self.assertRaises(ValueError): await pending
        self.assertIsNone(self.car.armed_session)
        self.assertEqual(self.car.next_message()['type'], 'stop')

    async def test_replayed_or_malformed_feedback_cannot_refresh_health(self):
        data = self.feedback()
        with self.assertRaises(ValueError): self.car.receive(data)
        for changes in [dict(uno_age_ms=1501.), dict(uno_age_ms=float('nan')),
                        dict(enabled=False), dict(seq=True), dict(permit='bad')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.feedback(**changes)
        self.now += .201
        self.assertEqual(self.car.health(), 'stale')


class RelaySocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_phone_feedback_cannot_starve_server_heartbeats(self):
        """Real sender/receiver tasks with the ESP's unsolicited 20 Hz permits.

        A healthy, disarmed phone still needs outbound heartbeats. Its inbound
        status must not keep resetting the server's heartbeat deadline.
        """
        class Socket:
            headers = {'authorization': 'Bearer ' + KEY}

            def __init__(self):
                self.incoming = asyncio.Queue()
                self.outgoing = asyncio.Queue()

            async def accept(self): pass
            async def close(self, code): pass
            async def receive_text(self): return await self.incoming.get()
            async def send_json(self, message): await self.outgoing.put(message)

        ws = Socket()
        car = RelayCar(KEY, fixture())
        car.identity = lambda: ('capture', 1)
        losses = []
        car.on_loss = losses.append
        serving = asyncio.create_task(car.serve(ws))

        async def feedback():
            seq = 0
            while True:
                seq += 1
                await ws.incoming.put(json.dumps(dict(version=1, type='status', seq=seq,
                    session_id='capture', map_epoch=1, permit=f'{seq:016X}',
                    uno_age_ms=100., enabled=True)))
                await asyncio.sleep(.05)

        feeding = asyncio.create_task(feedback())
        try:
            self.assertEqual((await asyncio.wait_for(ws.outgoing.get(), .5))['type'], 'stop')
            for _ in range(8):
                message = await asyncio.wait_for(ws.outgoing.get(), .35)
                self.assertEqual(message, dict(version=1, type='heartbeat'))
            self.assertTrue(car.connected)
            self.assertIsNone(car.armed_session)
            self.assertGreater(car.last_status_seq, 8)
            self.assertEqual(losses, [])
            # Server heartbeats must not hide missing phone/rover feedback.
            feeding.cancel()
            await asyncio.gather(feeding, return_exceptions=True)
            await asyncio.sleep(.8)
            self.assertTrue(car.connected, 'Idle Wi-Fi jitter must not close the setup link')
            self.assertEqual(car.health(), 'stale', 'Idle grace must not authorize motion')
            seq = car.last_status_seq + 1
            await ws.incoming.put(json.dumps(dict(version=1, type='status', seq=seq,
                session_id='capture', map_epoch=1, permit=f'{seq:016X}', uno_age_ms=100., enabled=True)))
            await asyncio.sleep(.02)
            self.assertEqual(car.health(), 'ok')
            self.assertIsNone(car.armed_session, 'Recovery cannot arm')
            await asyncio.wait_for(serving, 3.5)
            self.assertFalse(car.connected)
            self.assertEqual(losses, ['rover_relay_lost'])
        finally:
            feeding.cancel()
            await asyncio.gather(feeding, return_exceptions=True)
            # Let the real receive deadline retire the connection. This also
            # cleans up on a heartbeat assertion failure, without cancelling
            # the sender at the same instant feedback wakes it.
            await asyncio.wait_for(serving, 3.5)


class RelayHTTPTests(unittest.TestCase):
    def test_default_remains_logging_only(self):
        with TestClient(create_app(db_path=':memory:')) as client:
            self.assertEqual(client.get('/autonomy').json()['adapter'], 'logging')
            self.assertIn('logging_adapter_only', client.get('/autonomy').json()['blockers'])
            self.assertEqual(client.post('/arm').status_code, 409)
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/rover'):
                    pass

    def test_real_adapter_protects_motion_routes_and_exposes_measurement_blockers(self):
        car = RelayCar(KEY, fixture(forward=None))
        with TestClient(create_app(db_path=':memory:', car=car)) as client:
            for path in ('/arm', '/mode', '/manual', '/goal'):
                self.assertEqual(client.post(path, json={}).status_code, 401)
            self.assertEqual(client.post('/stop').status_code, 200)
            ready = client.get('/autonomy').json()
            self.assertFalse(ready['ready'])
            self.assertIn('actuation_forward_unmeasured', ready['blockers'])
            self.assertIn('rover_geometry_unmeasured', ready['blockers'])
            self.assertEqual(client.post('/mode', json={'mode': 'navigate'},
                headers={'Authorization': 'Bearer ' + KEY}).status_code, 200)
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect('/rover'):
                    pass

    def test_websocket_auth_single_owner_stop_barrier_and_disconnect(self):
        car = RelayCar(KEY, fixture())
        with TestClient(create_app(db_path=':memory:', car=car)) as client:
            with client.websocket_connect('/rover', headers={'Authorization': 'Bearer ' + KEY}) as ws:
                first = ws.receive_json()
                self.assertEqual(first['type'], 'stop')
                self.assertTrue(car.connected)
                self.assertEqual(car.health(), 'stale')  # write is NOT feedback
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect('/rover', headers={'Authorization': 'Bearer ' + KEY}):
                        pass
                ws.send_text(json.dumps({'version': 1, 'type': 'ack', 'id': 'Z' + first['id']}))
                self.assertEqual(ws.receive_json()['type'], 'heartbeat')
            self.assertFalse(car.connected)
            self.assertEqual(car.health(), 'down')

    def test_rgbd_map_goal_real_adapter_wire_and_stale_sensing_stop(self):
        """Full backend path with a synthetic phone and firmware acknowledgements.

        The real ESP parser/watchdog has separate host sanitizer tests. No
        measurements from this fixture describe the physical rover.
        """
        geometry = RoverCalibration.model_validate(dict(TEST_CALIBRATION, clearance_margin_m=.15))
        self.rehearse_rgbd(fixture(), geometry)

    def test_prototype_rgbd_goal_and_stale_sensing_stop(self):
        from backend.prototype import PrototypeActuation, prototype_geometry
        self.rehearse_rgbd(PrototypeActuation(), prototype_geometry(.24, .14))

    def rehearse_rgbd(self, actuation, geometry):
        car = RelayCar(KEY, actuation)
        app = create_app(db_path=':memory:', car=car, calibration=geometry,
                         detector=EmptyDetector(), capture_directory='')
        headers = {'Authorization': 'Bearer ' + KEY}
        with TestClient(app) as client, client.websocket_connect('/phone') as phone:
            source = PhoneScene(client, phone, session='relay-rehearsal')
            try:
                source.survey()
                source.start()
                with client.websocket_connect('/rover', headers=headers) as ws:
                    packets, errors = [], []
                    done = threading.Event()
                    def firmware():
                        seq = 0
                        try:
                            while not done.is_set():
                                message = ws.receive_json()
                                packets.append(message)
                                if message['type'] in ('stop', 'arm'):
                                    ack = ('Z' + message['id']) if message['type'] == 'stop' else ('A' + message['session'])
                                    ws.send_json(dict(version=1, type='ack', id=ack))
                                seq += 1
                                ws.send_json(dict(version=1, type='status', seq=seq,
                                    session_id=source.session, map_epoch=1, permit=f'{seq:016X}',
                                    uno_age_ms=100., enabled=True))
                        except Exception as error:
                            if not done.is_set(): errors.append(error)
                    worker = threading.Thread(target=firmware, daemon=True)
                    worker.start()
                    try:
                        wait_for(lambda: client.get('/autonomy').json()['ready'])
                        self.assertEqual(client.post('/mode', json={'mode': 'navigate'}, headers=headers).status_code, 200)
                        response = client.post('/arm', headers=headers)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertTrue(response.json()['armed'])
                        goal = client.post('/goal', json={'x': 0., 'z': .5}, headers=headers)
                        self.assertEqual(goal.status_code, 200, goal.text)
                        wait_for(lambda: any(p['type'] == 'command' and p['power'] > 0 for p in packets))
                        commands = [p for p in packets if p['type'] == 'command']
                        self.assertTrue(all(p['lease_ms'] == 200 and 0 <= p['power'] <= 80 for p in commands))
                        source.frames = False  # poses and firmware feedback stay healthy
                        wait_for(lambda: not app.state.armed)
                        self.assertEqual(app.state.stop_reason, 'sensing_stale')
                        stopped = len([p for p in packets if p['type'] == 'command'])
                        time.sleep(.25)
                        self.assertEqual(stopped, len([p for p in packets if p['type'] == 'command']))
                        self.assertIsNone(car.armed_session)
                        self.assertEqual(errors, [])
                    finally:
                        done.set()
                        worker.join(1)
            finally:
                source.close()


if __name__ == '__main__':
    unittest.main()
