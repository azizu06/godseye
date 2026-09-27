"""Dashboard manual authority through actual FastAPI/Motion/RelayCar; fake peer only."""
import asyncio
from dataclasses import replace
import time
import unittest

from backend.tests.test_persistent_explore import rig, eventually
from backend.manual_control import capabilities, clearance_problem
from backend.motion import MotionLimits
from backend.tests.test_actuation import fixture


class DashboardManualTests(unittest.IsolatedAsyncioTestCase):
    async def arm(self, client, mode='explore'):
        self.assertEqual((await client.post('/mode', json={'mode': mode})).status_code, 200)
        response = await client.post('/arm', params={'standard': mode == 'manual'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['motion_generation']

    async def acquire(self, client, generation):
        return await client.post('/manual', json=dict(v_mps=0., yaw_rate_rps=0.,
            takeover=True, expected_generation=generation))

    async def pulse(self, client, generation, v=.1, w=0., release=False):
        return await client.post('/manual', json=dict(v_mps=v, yaw_rate_rps=w,
                                                    expected_generation=generation, release=release))

    async def test_explicit_takeover_cancels_explore_without_rearming_and_release_stays_manual(self):
        async with rig() as (client, state, car, peer):
            old = await self.arm(client)
            session = car.armed_session
            response = await self.acquire(client, old)
            self.assertEqual(response.status_code, 200, response.text)
            generation = response.json()['motion_generation']
            self.assertGreater(generation, old)
            self.assertEqual(state.mode, 'manual')
            self.assertTrue(state.armed)
            self.assertFalse(state.auto_requested)
            self.assertFalse(state.nav.active)
            self.assertFalse(state.motion.submit(old, 'explore', .1, 0.))
            self.assertEqual((await self.pulse(client, generation)).status_code, 200)
            await eventually(lambda: any(p.get('direction', 0) for _, p in peer.packets))
            self.assertEqual((await self.pulse(client, generation, 0.)).status_code, 200)
            self.assertEqual(state.motion.generation, generation, 'held center is idle, not release')
            self.assertEqual((await self.pulse(client, generation)).status_code, 200)
            self.assertEqual((await self.pulse(client, generation, 0., release=True)).status_code, 200)
            self.assertEqual((await self.pulse(client, generation)).status_code, 409,
                             'a delayed pulse cannot revive a released gesture')
            await asyncio.sleep(.3)
            self.assertEqual(peer.packets[-1][1]['direction'], 0)
            self.assertEqual(car.armed_session, session)
            self.assertEqual(peer.count('arm'), 1)
            self.assertFalse(state.nav.active)

    async def test_standard_explicit_arm_and_unrenewed_lease(self):
        async with rig() as (client, state, car, peer):
            generation = (await self.acquire(client, await self.arm(client, 'manual'))).json()['motion_generation']
            self.assertEqual((await self.pulse(client, generation)).status_code, 200)
            await eventually(lambda: any(p.get('direction', 0) for _, p in peer.packets))
            await asyncio.sleep(.4)
            self.assertEqual(peer.packets[-1][1]['direction'], 0)
            self.assertTrue(state.armed)

    async def test_unarmed_unauthorized_stale_takeover_and_stop_rearm_cannot_revive(self):
        async with rig() as (client, state, car, peer):
            self.assertEqual((await self.acquire(client, 0)).status_code, 409)
            old = await self.arm(client)
            body = dict(v_mps=0., yaw_rate_rps=0., takeover=True, expected_generation=old)
            self.assertEqual((await client.post('/manual', json=body,
                             headers={'Authorization': 'Bearer wrong'})).status_code, 401)
            new = (await self.acquire(client, old)).json()['motion_generation']
            self.assertEqual((await self.acquire(client, old)).status_code, 409)
            self.assertEqual((await self.pulse(client, old)).status_code, 409)
            self.assertEqual((await self.pulse(client, old, 0., release=True)).status_code, 409)
            self.assertEqual(state.motion.generation, new, 'old release cannot retire the new controller')
            await client.post('/stop')
            self.assertEqual((await self.pulse(client, new)).status_code, 409)
            await self.arm(client, 'manual')
            self.assertEqual((await self.acquire(client, new)).status_code, 409)
            self.assertEqual((await self.pulse(client, new)).status_code, 409)
            self.assertFalse(state.auto_requested)

    async def test_takeover_rejects_pending_arm_and_nonzero_acquisition(self):
        async with rig() as (client, state, car, peer):
            generation = await self.arm(client)
            response = await client.post('/manual', json=dict(v_mps=.1, yaw_rate_rps=0.,
                takeover=True, expected_generation=generation))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(state.mode, 'explore')
            state.arm_request_token = object()
            self.assertEqual((await self.acquire(client, generation)).status_code, 409)
            self.assertEqual(state.motion.generation, generation)
            self.assertTrue(state.auto_requested)

    async def test_physical_obstacle_and_unsupported_reverse_block_without_adapter_error(self):
        async with rig() as (client, state, car, peer):
            generation = (await self.acquire(client, await self.arm(client))).json()['motion_generation']
            self.assertEqual((await self.pulse(client, generation, -.1)).status_code, 409)
            self.assertTrue(state.armed)
            peer.cells[40, 40] = 2
            await asyncio.sleep(.03)
            self.assertEqual((await self.pulse(client, generation)).status_code, 409)
            self.assertFalse(any(p.get('direction', 0) for _, p in peer.packets))
            self.assertEqual((await self.pulse(client, generation, 0.)).status_code, 200)

    async def test_new_obstacle_at_dispatch_and_disconnect_stop_authority(self):
        async with rig() as (client, state, car, peer):
            generation = (await self.acquire(client, await self.arm(client))).json()['motion_generation']
            self.assertEqual((await self.pulse(client, generation)).status_code, 200)
            peer.cells[40, 40] = 2
            state.autonomy_map = peer.snapshot()
            state.motion.tick()
            self.assertFalse(state.armed)
            self.assertFalse(state.auto_requested)
            self.assertFalse(any(p.get('direction', 0) for _, p in peer.packets))
            self.assertEqual((await self.pulse(client, generation)).status_code, 409)
            car.detach()
            self.assertEqual((await self.acquire(client, generation)).status_code, 409)

    async def test_scope_reset_and_stale_sensing_cannot_keep_manual_motion(self):
        for invalid in ('map', 'pose', 'reset'):
            with self.subTest(invalid=invalid):
                async with rig() as (client, state, car, peer):
                    generation = (await self.acquire(client, await self.arm(client))).json()['motion_generation']
                    self.assertEqual((await self.pulse(client, generation)).status_code, 200)
                    if invalid == 'reset':
                        await client.post('/session')
                    else:
                        if invalid == 'map':
                            state.autonomy_map = replace(peer.snapshot(), accepted_at=time.monotonic() - 2.)
                        else:
                            state.pose_at = time.monotonic() - 2.
                        state.motion.tick()
                    self.assertFalse(state.armed)
                    self.assertFalse(state.auto_requested)
                    self.assertEqual((await self.pulse(client, generation)).status_code, 409)

    async def test_unsupported_measured_arc_and_reverse_leave_armed_session_intact(self):
        async with rig() as (client, state, car, peer):
            generation = (await self.acquire(client, await self.arm(client))).json()['motion_generation']
            car.actuation = fixture()
            for v, w in ((.1, .2), (-.1, 0.), (.01, 0.)):
                response = await self.pulse(client, generation, v, w)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertTrue(state.armed)
            self.assertEqual((await self.pulse(client, generation, 0., .3)).status_code, 200)
            await eventually(lambda: any(p.get('direction', 0) for _, p in peer.packets))


class ManualCapabilityTests(unittest.TestCase):
    def test_measured_ranges_only_offer_supported_motion(self):
        self.assertEqual(capabilities(fixture(), MotionLimits()),
                         dict(forward=[.04, .15], reverse=None, yaw=[.18, .45], arcs=False))

    def test_reverse_rollout_checks_behind_not_a_forward_projection(self):
        from backend.navigator import NavSettings, RoverPose
        from backend.occupancy import OccupancySnapshot
        import numpy as np
        cells = np.ones((80, 80), dtype=np.uint8)
        cells[35, 40] = 2  # behind +Z-facing rover, clear initial footprint
        snapshot = OccupancySnapshot(('s', 1), 1, time.monotonic(), (), .1,
                                     (-2., -2.), .05, cells, 0.)
        pose = RoverPose(0., 0., 0., 0., 'normal')
        self.assertIsNone(clearance_problem(snapshot, pose, ('s', 1), NavSettings(), .2, 0.))
        self.assertEqual(clearance_problem(snapshot, pose, ('s', 1), NavSettings(), -.2, 0.),
                         'manual_path_blocked')
