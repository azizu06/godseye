"""Explore mission intent through real app, planner, motion and relay lifecycle.

The in-memory peer supplies synthetic sensing and ESP acknowledgements only;
no socket, phone, calibration measurement or hardware is involved.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
import time
from types import SimpleNamespace
import unittest

import httpx
import numpy as np

from backend.app import Pose, create_app
from backend.navigator import NavSettings
from backend.occupancy import OccupancySnapshot
from backend.prototype import PrototypeActuation, prototype_geometry
from backend.rover_relay import RelayCar
from backend.tests.test_objects import FakeDetector


async def eventually(predicate, timeout=3.):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Explore lifecycle did not reach the expected state')
        await asyncio.sleep(.005)


class Peer:
    def __init__(self, app, car):
        self.app, self.car = app, car
        self.sequence = 0
        self.acknowledge_arm = True
        self.packets = []
        self.cells = np.ones((80, 80), dtype=np.uint8)

    def snapshot(self):
        return OccupancySnapshot(self.app.state.session, 1, time.monotonic(), (), .18,
                                 (-2., -2.), .05, self.cells.copy(), 0.)

    async def run(self):
        state = self.app.state
        while True:
            self.sequence += 1
            state.pose = Pose(version=1, type='pose', session_id=state.session[0],
                map_epoch=1, frame_id=self.sequence, t_capture=float(self.sequence),
                t_wall_ms=int(time.time() * 1000), tracking='normal',
                transform=[-1.,0.,0.,0., 0.,1.,0.,0., 0.,0.,-1.,0., 0.,0.,0.,1.])
            state.pose_at = state.detected_at = time.monotonic()
            state.autonomy_map = self.snapshot()
            if self.car.connected:
                # A fresh sequence/permit follows every Stop/Arm acknowledgement,
                # exercising the real RelayCar.prepare handshake barrier.
                self.car.receive(dict(version=1, type='status', seq=self.sequence,
                    session_id=state.session[0], map_epoch=1,
                    permit=f'{self.sequence:016X}', uno_age_ms=0., enabled=True))
                while (packet := self.car.next_message()) is not None:
                    self.packets.append((time.monotonic(), packet))
                    if packet['type'] == 'stop':
                        self.car.receive(dict(version=1, type='ack', id='Z' + packet['id']))
                    elif packet['type'] == 'arm' and self.acknowledge_arm:
                        self.car.receive(dict(version=1, type='ack', id='A' + packet['session']))
            await asyncio.sleep(.01)

    def count(self, kind):
        return sum(packet['type'] == kind for _, packet in self.packets)


@asynccontextmanager
async def rig(*, no_progress_s=100.):
    car = RelayCar('TEST_KEY_NOT_REAL_01234567890123456789', PrototypeActuation())
    app = create_app(db_path=':memory:', car=car, detector=FakeDetector(), capture_directory='',
        calibration=prototype_geometry(.24, .14),
        nav_settings=NavSettings(rate_hz=100., no_progress_s=no_progress_s))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                    headers={'Authorization': 'Bearer ' + car.key}) as client:
            await client.post('/session')
            app.state.phone = object()
            peer = Peer(app, car)
            app.state.occupancy = SimpleNamespace(session=app.state.session, map_snapshot=peer.snapshot)
            car.attach()
            source = asyncio.create_task(peer.run())
            try:
                await eventually(lambda: car.health() == 'ok' and app.state.pose is not None)
                yield client, app.state, car, peer
            finally:
                source.cancel()
                with suppress(asyncio.CancelledError):
                    await source


class PersistentExploreTests(unittest.IsolatedAsyncioTestCase):
    async def select_and_arm(self, client):
        self.assertEqual((await client.post('/mode', json={'mode': 'explore'})).status_code, 200)
        response = await client.post('/arm')
        self.assertEqual(response.status_code, 200, response.text)

    async def test_first_failed_explore_arm_retains_intent_and_recovers(self):
        async with rig() as (client, state, car, peer):
            await client.post('/mode', json={'mode': 'explore'})
            car.detach()
            response = await client.post('/arm')
            self.assertEqual(response.status_code, 409)
            self.assertTrue((await client.get('/autonomy')).json()['auto_requested'])
            self.assertFalse(state.armed)
            car.attach()
            await eventually(lambda: state.armed)
            self.assertGreaterEqual(peer.count('arm'), 1)

    async def test_no_progress_stops_motors_then_waits_before_retrying_the_mission(self):
        async with rig(no_progress_s=.12) as (client, state, car, peer):
            await self.select_and_arm(client)
            await eventually(lambda: state.stop_reason == 'no_progress')
            self.assertTrue(state.auto_requested)
            self.assertFalse(state.armed)
            self.assertIsNone(car.armed_session)
            arms = peer.count('arm')
            await asyncio.sleep(.25)
            self.assertEqual(peer.count('arm'), arms, 'no-progress recovery must back off after the stop')
            await eventually(lambda: peer.count('arm') > arms)

    async def test_no_operator_request_never_arms_on_recovery(self):
        async with rig() as (client, state, car, peer):
            await client.post('/mode', json={'mode': 'explore'})
            car.detach()
            car.attach()
            await asyncio.sleep(.3)
            self.assertFalse(state.auto_requested)
            self.assertFalse(state.armed)
            self.assertEqual(peer.count('arm'), 0)

    async def test_first_handshake_loss_preserves_request_and_recovers(self):
        async with rig() as (client, state, car, peer):
            await client.post('/mode', json={'mode': 'explore'})
            peer.acknowledge_arm = False
            attempt = asyncio.create_task(client.post('/arm'))
            await eventually(lambda: peer.count('arm') == 1)
            car.detach()
            self.assertEqual((await attempt).status_code, 409)
            self.assertTrue(state.auto_requested)
            self.assertFalse(state.armed)
            peer.acknowledge_arm = True
            car.attach()
            await eventually(lambda: state.armed)
            self.assertEqual(peer.count('arm'), 2)

    async def test_no_frontier_waits_and_explores_when_new_floor_opens(self):
        async with rig() as (client, state, car, peer):
            peer.cells[[0, -1], :] = 2
            peer.cells[:, [0, -1]] = 2
            await self.select_and_arm(client)
            await eventually(lambda: state.nav.active)
            await asyncio.sleep(.3)
            self.assertTrue(state.auto_requested)
            self.assertTrue(state.armed)
            self.assertFalse(any(packet.get('direction', 0) for _, packet in peer.packets))
            peer.cells = np.ones((80, 80), dtype=np.uint8)
            await eventually(lambda: any(packet.get('direction', 0) for _, packet in peer.packets), timeout=5.)
            self.assertEqual(peer.count('arm'), 1, 'new floor should resume the same Explore run')

    async def test_explicit_cancellation_wins_over_pending_recovery_handshake(self):
        for path, body in [('/stop', None), ('/mode', {'mode': 'manual'}),
                           ('/device/action', {'action': 'control_disable'})]:
            with self.subTest(path=path):
                async with rig() as (client, state, car, peer):
                    await self.select_and_arm(client)
                    car.detach()
                    peer.acknowledge_arm = False
                    car.attach()
                    await eventually(lambda: peer.count('arm') == 2)
                    pending_session = next(packet['session'] for _, packet in reversed(peer.packets)
                                           if packet['type'] == 'arm')
                    await client.post(path, json=body)
                    car.receive(dict(version=1, type='ack', id='A' + pending_session))
                    peer.acknowledge_arm = True
                    await asyncio.sleep(.2)
                    self.assertFalse(state.auto_requested)
                    self.assertFalse(state.armed)
                    self.assertIsNone(car.armed_session)
                    self.assertEqual(peer.count('arm'), 2)

    async def test_shutdown_drains_the_pending_recovery_task(self):
        async with rig() as (client, state, car, peer):
            await self.select_and_arm(client)
            car.detach()
            peer.acknowledge_arm = False
            car.attach()
            await eventually(lambda: peer.count('arm') == 2)
            pending = state.auto_arm_task
            self.assertFalse(pending.done())
        self.assertTrue(pending.done(), 'shutdown must await the recovery task before closing the database')
        self.assertFalse(state.auto_requested)
        self.assertIsNone(car.armed_session)
