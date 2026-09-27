"""Deterministic capture pacing: measured evidence, bounded pauses, no motion authority."""
from dataclasses import replace
import math
import unittest

from backend.scan_pacing import CameraPose, ScanObservation, ScanPacer


def camera(t, x=0., y=1., yaw=0., scope=('scan', 1), owner=1):
    c, s = math.cos(yaw), math.sin(yaw)
    return CameraPose(scope, owner, t, (x, y, 0.), (c, 0., -s, 0., 1., 0., s, 0., c))


def observation(t, now, pose=None, keys=None, usable=True):
    return ScanObservation(pose or camera(t), int(t * 1000), now,
                           frozenset((i, 0, 0) for i in range(100)) if keys is None else keys,
                           usable)


class PacingTests(unittest.TestCase):
    def test_four_hz_phase_jitter_gets_stable_evidence_then_departs_early(self):
        pacer = ScanPacer()
        self.assertTrue(pacer.step(0., camera(0.), None, True))
        self.assertTrue(pacer.step(.51, camera(.51), None, True))
        for t in (.76, 1.03):
            self.assertTrue(pacer.step(t, camera(t), observation(t, t), True))
        self.assertFalse(pacer.step(1.29, camera(1.29), observation(1.29, 1.29), True))
        self.assertEqual(pacer.status['result'], 'stable_support')
        self.assertEqual(pacer.status['stable_frames'], 3)
        self.assertFalse(pacer.step(8., camera(8.), observation(8., 8.), True))
        self.assertTrue(pacer.step(8.1, camera(8.1, x=1.1), None, True))

    def test_duplicate_queued_and_sparse_frames_never_credit_then_timeout_does_not_repeat(self):
        pacer = ScanPacer()
        pacer.step(0., camera(10.), None, True)
        pacer.step(.51, camera(10.51), None, True)
        for now in (.6, .8, 1., 1.2):
            self.assertTrue(pacer.step(now, camera(10.51 + now), observation(10.4, now), True))
        self.assertTrue(pacer.step(1.3, camera(11.3), observation(11.3, 1.3, usable=False), True))
        self.assertTrue(pacer.step(1.4, camera(11.4), observation(11.4, 1.4), True))
        self.assertTrue(pacer.step(1.5, camera(11.5), observation(11.4, 1.4), True))
        self.assertFalse(pacer.step(2., camera(12.), None, True))
        self.assertEqual(pacer.status['result'], 'capture_limited')
        self.assertEqual(pacer.status['stable_frames'], 1)
        self.assertFalse(pacer.step(20., camera(30.), None, True))

    def test_full_camera_drift_resets_settle_and_hard_deadline_remains(self):
        pacer = ScanPacer()
        pacer.step(0., camera(0.), None, True)
        pacer.step(.51, camera(.51), None, True)
        pacer.step(.8, camera(.8), observation(.8, .8), True)
        self.assertTrue(pacer.step(.9, camera(.9, y=1.5), None, True))
        self.assertEqual(pacer.status['stable_frames'], 0)
        pacer.step(1.41, camera(1.41, y=1.5), None, True)
        moving = observation(1.6, 1.6, pose=camera(1.6, y=1.))
        self.assertTrue(pacer.step(1.6, camera(1.6, y=1.5), moving, True))
        self.assertEqual(pacer.status['stable_frames'], 0)
        self.assertFalse(pacer.step(2., camera(2., y=1.5), None, True))

    def test_turn_obstacle_and_owner_change_preempt_checkpoint(self):
        for changed in ('blocked', 'owner', 'scope'):
            with self.subTest(changed=changed):
                pacer = ScanPacer()
                pacer.step(0., camera(0.), None, True)
                pose = camera(.3, owner=2) if changed == 'owner' else camera(.3)
                if changed == 'scope':
                    pose = camera(.3, scope=('new', 1))
                self.assertFalse(pacer.step(.3, pose, None, changed != 'blocked'))
                self.assertEqual(pacer.status['result'], 'interrupted')

    def test_low_cadence_and_repeated_mesh_do_not_imply_success(self):
        pacer = ScanPacer()
        pacer.step(0., camera(0.), None, True)
        pacer.step(.5, camera(.5), None, True)
        pacer.step(1., camera(1.), observation(1., 1.), True)
        self.assertFalse(pacer.step(2., camera(2.), observation(2., 2.), True))
        self.assertEqual(pacer.status['result'], 'capture_limited')
        self.assertLess(pacer.status['stable_frames'], 3)

    def test_stale_observation_and_continued_new_support_cannot_claim_saturation(self):
        pacer = ScanPacer()
        pacer.step(0., camera(0.), None, True)
        pacer.step(.5, camera(.5), None, True)
        pacer.step(.8, camera(.8), observation(.8, -2.), True)
        self.assertEqual(pacer.status['stable_frames'], 0)
        for i, t in enumerate((1., 1.25, 1.5)):
            keys = frozenset((x + 1000 * i, 0, 0) for x in range(100))
            self.assertTrue(pacer.step(t, camera(t), observation(t, t, keys=keys), True))
        self.assertFalse(pacer.step(2., camera(2.), None, True))
        self.assertEqual(pacer.status['result'], 'capture_limited')


    def test_frozen_pose_clock_and_pitch_roll_changes_do_not_finish_settling(self):
        pacer = ScanPacer()
        pacer.step(0., camera(10.), None, True)
        pacer.step(.7, camera(10.), observation(10.7, .7), True)
        self.assertEqual(pacer.status['phase'], 'settling')
        self.assertEqual(pacer.status['stable_frames'], 0)
        # Rotate around X: camera height/XZ/yaw alone cannot attest a stable view.
        c, s = math.cos(.3), math.sin(.3)
        tilted = replace(camera(10.8), rotation=(1., 0., 0., 0., c, -s, 0., s, c))
        pacer.step(.8, tilted, None, True)
        pacer.step(1.4, replace(tilted, captured_at=11.4), None, True)
        pacer.step(1.6, replace(tilted, captured_at=11.6), observation(11.6, 1.6), True)
        self.assertEqual(pacer.status['stable_frames'], 0)


class NavigatorPacingTests(unittest.IsolatedAsyncioTestCase):
    async def run_route(self, paced, interrupt=None):
        import asyncio
        import time
        from backend.scan_pacing import CameraPose
        from backend.tests.test_navigator import Harness, Rover, wait_until
        from backend.tests.test_obstacle_replanning import ObservedRoom
        from backend.occupancy import frame_evidence
        from backend.tests.test_occupancy import box, FLOOR_Y

        room = ObservedRoom()
        rover = Rover(2., 1., 0., sim_dt=.02)
        h = Harness(rover, room, mode='explore', rate_hz=50., replan_s=4., no_progress_s=.4)
        start = time.monotonic()
        latest = [None]
        stable_frames = [0]
        base_pose = rover.pose

        def full_pose():
            p = base_pose()
            t = time.monotonic() - start
            c, s = math.cos(p.yaw_rad), math.sin(p.yaw_rad)
            camera_pose = CameraPose(('obstacle-test', 1), 1, t, (p.x, 1., p.z),
                                     (c, 0., -s, 0., 1., 0., s, 0., c))
            return replace(p, camera=camera_pose)

        h.nav._pose = full_pose
        h.nav._scan_observation = (lambda: latest[0]) if paced else None

        async def capture():
            previous = None
            while True:
                p = full_pose().camera
                stationary = previous is not None and math.dist(p.position, previous.position) < .005
                if stationary and rover.commands and rover.commands[-1] == (0., 0.):
                    stable_frames[0] += 1
                latest[0] = observation(p.captured_at, time.monotonic(), pose=p)
                previous = p
                await asyncio.sleep(.25)

        sensing = asyncio.create_task(room.sensing())
        captures = asyncio.create_task(capture())
        try:
            h.nav._begin('explore', (2., 3.5), None, h.generation)
            if interrupt:
                await wait_until(lambda: h.nav.scan_status and h.nav.scan_status['phase'] == 'settling')
                if interrupt == 'obstacle':
                    points = box(1.9, 2.1, 1.05, 1.2, FLOOR_Y, FLOOR_Y + .4, step=.04)
                    room.grid.commit(frame_evidence(room.points), time.monotonic(),
                                     mesh_keys=frame_evidence(points).keys)
                    await wait_until(lambda: h.nav.waiting_reason == 'start_blocked')
                    self.assertEqual(h.nav.scan_status['result'], 'interrupted')
                    self.assertEqual(h.nav.scan_status['phase'], 'moving')
                    before = len(rover.commands)
                    await asyncio.sleep(.2)
                    self.assertTrue(all(c == (0., 0.) for c in rover.commands[before:]))
                else:
                    if interrupt == 'generation':
                        h.generation += 1
                        await h.finished(timeout=1.)
                        self.assertEqual(h.stops, ['command_stale'])
                    else:
                        h.stop(interrupt)
                    before = len(rover.commands)
                    await asyncio.sleep(.3)
                    self.assertEqual(rover.commands[before:], [])
                    self.assertIsNone(h.nav.scan_status)
            else:
                await asyncio.sleep(2.3)
                self.assertEqual(h.stops, [])
                self.assertGreater(rover.z, 1.05)
                if paced:
                    self.assertEqual(h.nav.scan_status['result'], 'stable_support')
                    self.assertEqual(h.nav.scan_status['checkpoints'], 1)
                return stable_frames[0], rover.z, list(rover.commands)
        finally:
            await h.nav.aclose()
            for task in (sensing, captures):
                task.cancel()
            await asyncio.gather(sensing, captures, return_exceptions=True)

    async def test_actual_navigator_ab_obtains_stable_frames_and_still_makes_progress(self):
        baseline = await self.run_route(False)
        paced = await self.run_route(True)
        self.assertEqual(baseline[0], 0)
        self.assertGreaterEqual(paced[0], 3)
        self.assertGreater(baseline[1], paced[1])  # intentional bounded capture cost
        self.assertTrue(any(v > 0 for v, _ in paced[2]))
        print('Pacing A/B: stable frames', baseline[0], '->', paced[0],
              '; traveled m', round(baseline[1] - 1., 3), '->', round(paced[1] - 1., 3))

    async def test_actual_obstacle_and_stop_preempt_active_checkpoint(self):
        for interrupt in ('obstacle', 'operator_stop', 'session_reset', 'phone_disconnected', 'generation'):
            with self.subTest(interrupt=interrupt):
                await self.run_route(True, interrupt)
