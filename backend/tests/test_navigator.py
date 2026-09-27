"""Goal/explore runner proof with a kinematic stand-in rover; the app tests use FakeCar."""
import asyncio
import math
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

import backend.app
import backend.navigator
from backend.app import create_app
from backend.drive import FakeCar
from backend.motion import MotionLimits
from backend.navigation import FollowerConfig, Grid, PlannerConfig, path_blocked, plan_path
from backend.navigator import Navigator, NavSettings, RoverPose
from backend.occupancy import CELL_M, OccupancySnapshot
from tools.car_rehearsal import TEST_CALIBRATION, PhoneScene, wait_for
from backend.calibration import RoverCalibration
from backend.tests.test_objects import FakeDetector

UNKNOWN, FREE, OCCUPIED = 0, 1, 2
# 200 Hz ticks with a 0.1 s simulated step: a 10 Hz controller running 20x faster than
# real time, so replan_s of 0.05 real seconds is about one replan per simulated second.
FAST = dict(rate_hz=200., replan_s=.05, blocked_check_s=.01, no_progress_s=1.)


def cells(fill=FREE, size=60):
    return np.full((size, size), fill, dtype=np.uint8)


def walled(base):
    """A wall across x = 1.45-1.55 m from z = 0 to 2.2 m, open above it."""
    out = base.copy()
    out[0:44, 29:31] = OCCUPIED
    return out


def enclosed():
    out = cells()
    out[0, :] = out[-1, :] = out[:, 0] = out[:, -1] = OCCUPIED
    return out


def initial_plan(grid_cells, start, goal, config=PlannerConfig()):
    from dataclasses import replace
    config = replace(config, unknown_traversable=False, footprint_clearance=True,
                     snap_radius_m=0., start_snap_radius_m=0.)
    result = plan_path(Grid.from_array(grid_cells, origin=(0., 0.), cell_m=CELL_M), start, goal, config)
    assert result.ok, result.reason
    return result


async def wait_until(condition, timeout=10.):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError('timed out waiting for the navigator')
        await asyncio.sleep(.005)


class FakeOccupancy:
    """The two OccupancyGrid members the navigator reads: revision and snapshot()."""

    def __init__(self, grid_cells=None):
        self.revision, self.cells, self.fail = 0, None, False
        self.set(grid_cells)

    def set(self, grid_cells):
        self.cells = None if grid_cells is None else np.array(grid_cells, dtype=np.uint8)
        self.revision += 1

    def snapshot(self):
        if self.fail:
            raise RuntimeError('snapshot failed')
        return OccupancySnapshot(('TEST', 1), self.revision, time.monotonic(),
                                 () if self.cells is not None else ('no_floor',), .18,
                                 (0., 0.), CELL_M, self.cells, 0.)


class Rover:
    """Pose source plus car: each drive command advances the pose by sim_dt of motion."""

    def __init__(self, x, z, yaw, *, moves=True, sim_dt=.1, on_move=None):
        self.x, self.z, self.yaw = x, z, yaw
        self.moves, self.sim_dt, self.on_move = moves, sim_dt, on_move
        self.age_s, self.tracking, self.present = 0., 'normal', True
        self.commands, self.trace = [], []

    def pose(self):
        if not self.present:
            return None
        return RoverPose(self.x, self.z, self.yaw, self.age_s, self.tracking)

    def drive(self, v, w):
        self.commands.append((v, w))
        if self.moves:
            self.yaw += w * self.sim_dt
            self.x += v * math.sin(self.yaw) * self.sim_dt
            self.z += v * math.cos(self.yaw) * self.sim_dt
            self.trace.append((self.x, self.z))
            if self.on_move is not None:
                self.on_move(self)


class Harness:
    def __init__(self, rover, occupancy, mode='navigate', pause_reason=None, **settings):
        self.rover, self.occupancy = rover, occupancy
        self.stops, self.paths, self.modes = [], [], set()
        self.armed, self.mode, self.generation = True, mode, 1
        self.nav = Navigator(NavSettings(**{**FAST, "follower": FollowerConfig(lookahead_m=.1), **settings}), pose=rover.pose,
                             occupancy=self.occupancy.snapshot, submit=self.submit, stop=self.stop,
                             publish=self.publish, armed_mode=lambda: self.mode if self.armed else None,
                             pause_reason=pause_reason)

    def submit(self, generation, mode, v, w):
        """Mirrors backend.motion.Motion.submit: only the armed generation reaches the car."""
        if not self.armed or generation != self.generation:
            return False
        self.modes.add(mode)
        self.rover.drive(v, w)
        return True

    def stop(self, reason):
        """Mirrors backend.app stop(): disarm, end the generation, halt the run, then a zero drive."""
        self.armed = False
        self.generation += 1
        self.stops.append(reason)
        self.nav.halt()
        self.rover.drive(0., 0.)

    def publish(self, message):
        assert message['type'] == 'path' and message['version'] == 1, message
        self.paths.append(message['points'])

    async def finished(self, timeout=10.):
        await wait_until(lambda: not self.nav.active, timeout)


class NavigatorTests(unittest.IsolatedAsyncioTestCase):
    def assert_within_limits(self, commands):
        for v, w in commands:
            self.assertTrue(0. <= v <= .2 and abs(w) <= .5, (v, w))

    def assert_stopped(self, h, reason):
        self.assertEqual(h.stops, [reason])
        self.assertFalse(h.nav.active)
        self.assertEqual(h.rover.commands[-1], (0., 0.))
        self.assertEqual(h.nav.path, [])
        if h.paths:
            self.assertEqual(h.paths[-1], [])

    async def test_goal_run_follows_the_path_to_arrival_and_clears_it(self):
        occupancy = FakeOccupancy(walled(cells()))
        start, goal = (.5, .5), (2.5, .5)
        h = Harness(Rover(*start, math.pi / 2), occupancy)
        initial = initial_plan(occupancy.cells, start, goal)
        h.nav.start_goal(goal, initial, h.generation)
        self.assertTrue(h.nav.active)
        await h.finished()
        self.assert_stopped(h, 'arrived')
        self.assertEqual(h.paths[0], initial.points)
        self.assertLessEqual(math.hypot(h.rover.x - goal[0], h.rover.z - goal[1]), .15)
        self.assert_within_limits(h.rover.commands)
        self.assertTrue(any(v > 0 for v, _ in h.rover.commands))
        for x, z in h.rover.trace:  # went over the wall, never through it
            if 1.3 < x < 1.7:
                self.assertGreater(z, 2.2)

    async def test_a_command_refused_by_motion_ends_the_run_and_clears_the_path(self):
        occupancy = FakeOccupancy(walled(cells()))
        start, goal = (.5, .5), (2.5, .5)
        h = Harness(Rover(*start, math.pi / 2), occupancy)
        h.nav.start_goal(goal, initial_plan(occupancy.cells, start, goal), h.generation)
        await wait_until(lambda: len(h.rover.commands) >= 3)
        h.generation += 1  # e.g. re-armed while this run kept going: its commands are now stale
        moved = len(h.rover.commands)
        await h.finished()
        self.assert_stopped(h, 'command_stale')
        self.assertEqual(h.rover.commands[moved:], [(0., 0.)])  # only the stop's zero
        self.assertEqual(h.modes, {'navigate'})

    async def test_wall_seen_mid_run_replans_a_detour_and_arrives(self):
        occupancy = FakeOccupancy(cells())
        start, goal = (.5, .5), (2.5, .5)
        initial = initial_plan(occupancy.cells, start, goal)
        def reveal_wall(rover):
            if rover.x > .9 and occupancy.revision == 1:
                occupancy.set(walled(occupancy.cells))
        h = Harness(Rover(*start, math.pi / 2, on_move=reveal_wall), occupancy, replan_s=100.)
        h.nav.start_goal(goal, initial, h.generation)
        await h.finished()
        self.assert_stopped(h, 'arrived')
        self.assertEqual(occupancy.revision, 2)
        for x, z in h.rover.trace:
            if 1.3 < x < 1.7:
                self.assertGreater(z, 2.2)

    async def test_map_update_that_seals_the_goal_stops_with_no_path(self):
        occupancy = FakeOccupancy(cells())
        start, goal = (.5, .5), (2.5, .5)
        h = Harness(Rover(*start, math.pi / 2), occupancy, replan_s=100.)
        initial = initial_plan(occupancy.cells, start, goal)
        sealed = cells()
        sealed[:, 29:31] = OCCUPIED  # both points are inside the grid, so no unknown padding
        occupancy.set(sealed)
        h.nav.start_goal(goal, initial, h.generation)
        await h.finished()
        self.assert_stopped(h, 'no_path')

    async def test_goal_turning_occupied_stops_as_destination_blocked(self):
        occupancy = FakeOccupancy(cells())
        start, goal = (.5, .5), (2.5, .5)
        h = Harness(Rover(*start, math.pi / 2), occupancy)
        initial = initial_plan(occupancy.cells, start, goal)
        blocked = cells()
        blocked[5:15, 45:55] = OCCUPIED
        occupancy.set(blocked)
        h.nav.start_goal(goal, initial, h.generation)
        await h.finished()
        self.assert_stopped(h, 'destination_blocked')

    async def test_replan_hitting_the_search_limit_stops(self):
        occupancy = FakeOccupancy(walled(cells()))
        start, goal = (.5, .5), (2.5, .5)
        initial = initial_plan(occupancy.cells, start, goal)
        h = Harness(Rover(*start, math.pi / 2), occupancy, planner=PlannerConfig(max_expansions=20))
        h.nav.start_goal(goal, initial, h.generation)
        await h.finished()
        self.assert_stopped(h, 'search_limit')

    async def test_pose_problems_stop_before_any_motion(self):
        cases = [('pose_stale', lambda r: setattr(r, 'age_s', 1.)),
                 ('pose_stale', lambda r: setattr(r, 'present', False)),
                 ('tracking_lost', lambda r: setattr(r, 'tracking', 'limited'))]
        for reason, spoil in cases:
            with self.subTest(reason=reason):
                occupancy = FakeOccupancy(cells())
                rover = Rover(.5, .5, math.pi / 2)
                spoil(rover)
                h = Harness(rover, occupancy)
                h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)), h.generation)
                await h.finished()
                self.assert_stopped(h, reason)
                self.assertEqual(set(rover.commands), {(0., 0.)})

    async def test_pose_going_stale_mid_run_stops(self):
        occupancy = FakeOccupancy(cells())
        rover = Rover(.5, .5, math.pi / 2)
        h = Harness(rover, occupancy)
        h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)), h.generation)
        await wait_until(lambda: any(v > 0 for v, _ in rover.commands))
        rover.age_s = .3
        await h.finished()
        self.assert_stopped(h, 'pose_stale')

    async def test_prototype_explore_pauses_then_replans_after_feedback_recovers(self):
        occupancy = FakeOccupancy(cells(UNKNOWN))
        occupancy.cells[5:25, 5:55] = FREE
        rover = Rover(1.5, .75, 0., moves=False)
        paused = [None]
        h = Harness(rover, occupancy, mode='explore', pause_reason=lambda: paused[0],
                    no_progress_s=10.)
        self.addAsyncCleanup(h.nav.aclose)
        h.nav.start_explore(h.generation)
        await wait_until(lambda: any(v or w for v, w in rover.commands))
        paused[0] = 'car_stale'
        await wait_until(lambda: rover.commands[-1] == (0., 0.))
        count = len(rover.commands)
        await asyncio.sleep(.05)
        self.assertEqual(h.stops, [])
        self.assertEqual(rover.commands[count:], [])
        paused[0] = None
        await wait_until(lambda: any(v or w for v, w in rover.commands[count:]))
        self.assertEqual(h.stops, [])

    async def test_prototype_explore_remains_armed_through_a_long_pause(self):
        h = Harness(Rover(.5, .5, 0.), FakeOccupancy(cells()), mode='explore',
                    pause_reason=lambda: 'car_stale')
        self.addAsyncCleanup(h.nav.aclose)
        h.nav.start_explore(h.generation)
        await asyncio.sleep(.15)
        self.assertTrue(h.nav.active)
        self.assertEqual(h.stops, [])
        self.assertFalse(any(v or w for v, w in h.rover.commands))

    async def test_commanded_motion_without_pose_change_trips_the_no_progress_watchdog(self):
        occupancy = FakeOccupancy(cells())
        rover = Rover(.5, .5, math.pi / 2, moves=False)  # the logging-only car never moves
        h = Harness(rover, occupancy, no_progress_s=.2)
        h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)), h.generation)
        await h.finished()
        self.assert_stopped(h, 'no_progress')
        self.assertTrue(any(v > 0 for v, _ in rover.commands[:-1]))

    async def test_external_stop_ends_driving_immediately_and_clears_the_path(self):
        occupancy = FakeOccupancy(cells())
        rover = Rover(.5, .5, math.pi / 2)
        h = Harness(rover, occupancy)
        h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)), h.generation)
        await wait_until(lambda: len(rover.commands) > 5)
        h.stop('operator_stop')  # what /stop, /mode, /session and phone loss all call
        sent = len(rover.commands)
        await asyncio.sleep(.05)
        self.assertEqual(len(rover.commands), sent)
        self.assert_stopped(h, 'operator_stop')

    async def test_disarm_that_skipped_halt_is_still_caught_at_the_next_tick(self):
        for spoil in (lambda h: setattr(h, 'armed', False), lambda h: setattr(h, 'mode', 'manual')):
            occupancy = FakeOccupancy(cells())
            h = Harness(Rover(.5, .5, math.pi / 2), occupancy)
            h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)), h.generation)
            await wait_until(lambda: len(h.rover.commands) > 2)
            spoil(h)
            await h.finished()
            self.assert_stopped(h, 'disarmed')

    async def test_planning_errors_stop_with_nav_error(self):
        occupancy = FakeOccupancy(cells())
        h = Harness(Rover(.5, .5, math.pi / 2), occupancy)
        h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)), h.generation)
        occupancy.fail = True
        with self.assertLogs('backend.navigator', level='ERROR'):
            await h.finished()
        self.assert_stopped(h, 'nav_error')

    async def test_unchanged_replans_publish_the_path_once(self):
        occupancy = FakeOccupancy(cells())
        rover = Rover(.5, .5, math.pi / 2, moves=False)
        h = Harness(rover, occupancy, no_progress_s=100.)
        initial = initial_plan(occupancy.cells, (.5, .5), (2.5, .5))
        h.nav.start_goal((2.5, .5), initial, h.generation)
        await asyncio.sleep(.3)  # about six replans from the same pose
        self.assertEqual(h.paths, [initial.points])
        self.assertEqual(h.nav.path, initial.points)
        await h.nav.aclose()
        self.assertFalse(h.nav.active)
        self.assertEqual(h.paths, [initial.points, []])

    async def test_explore_refuses_a_map_without_floor(self):
        h = Harness(Rover(1.5, .75, 0.), FakeOccupancy(None), mode='explore')
        h.nav.start_explore(h.generation)
        await h.finished()
        self.assert_stopped(h, 'no_floor')
        self.assertFalse(any(v or w for v, w in h.rover.commands))

    async def test_explore_visits_frontiers_and_idles_when_the_map_is_closed(self):
        await self.explore_visits_frontiers_and_closes()

    async def test_explore_advances_frontiers_when_every_tick_replans(self):
        # Force a plan for the old frontier to be pending on its arrival tick.
        # This used to resurrect that frontier forever instead of choosing the next.
        await self.explore_visits_frontiers_and_closes(replan_s=0.)

    async def explore_visits_frontiers_and_closes(self, **settings):
        corridor = cells(UNKNOWN)
        corridor[5:25, 5:55] = FREE  # explored strip; everything around it is unknown
        occupancy = FakeOccupancy(corridor)
        rover = Rover(1.5, .75, 0.)
        h = Harness(rover, occupancy, mode='explore', **settings)
        self.addAsyncCleanup(h.nav.aclose)
        h.nav.start_explore(h.generation)
        # Two distinct path ends: it reached one frontier and moved on to the next.
        await wait_until(lambda: len({tuple(p[-1]) for p in h.paths if p}) >= 2)
        grid = Grid.from_array(corridor, origin=(0., 0.), cell_m=CELL_M)
        for points in h.paths:
            if points:
                row, col = grid.world_to_cell(*points[-1])
                self.assertEqual(corridor[row, col], FREE)
        occupancy.set(enclosed())
        await wait_until(lambda: h.nav.path == [] and rover.commands[-1] == (0., 0.))
        self.assertTrue(h.nav.active)
        self.assertEqual(h.stops, [])
        self.assert_within_limits(rover.commands)


class LiveNavigationTests(unittest.TestCase):
    """/goal, path publishing and app-level stops through the real /arm. FakeCar reports
    ok and records what the motion pump sends; no frames are streamed, so the tests
    refresh the detector's last-result time to stand in for a healthy detector."""

    def setUp(self):
        self.t_capture = 100.

    def app(self, **settings):
        self.car = FakeCar()
        self.backend = create_app(':memory:', car=self.car, detector=FakeDetector(),
                                  calibration=RoverCalibration.model_validate(TEST_CALIBRATION), **settings)
        return self.backend

    def stream(self, phone, seconds):
        deadline = time.monotonic() + seconds
        self.keep_fresh(phone, lambda: time.monotonic() > deadline)

    def sends(self):
        return [call[1:] for call in self.car.calls if call[0] == 'send']

    def hello(self):
        return dict(version=1, type='hello', device='test-phone', session_id='nav-session',
                    map_epoch=1, supports_scene_depth=True, supports_mesh=True)

    def pose(self, tracking='normal'):
        # Pose at the surveyed origin, camera facing +Z (yaw zero).
        self.t_capture += .02
        return dict(version=1, type='pose', session_id='nav-session', map_epoch=1, frame_id=1,
                    t_capture=self.t_capture, t_wall_ms=int(time.time() * 1000),
                    transform=[-1., 0., 0., 0., 0., 1., 0., 0., 0., 0., -1., 0., 0., 0., 0., 1.],
                    tracking=tracking)

    def keep_fresh(self, phone, condition, timeout=3.):
        """Stream poses (so no watchdog fires) until condition() holds."""
        deadline = time.monotonic() + timeout
        while True:
            phone.send_json(self.pose())
            self.backend.state.detected_at = time.monotonic()
            if condition():
                return
            if time.monotonic() > deadline:
                self.fail('timed out')
            time.sleep(.02)

    def arm(self, client, phone, mode):
        wait_for(lambda: client.app.state.occupancy is not None)
        if client.app.state.occupancy.revision == 0:
            source = PhoneScene(client, phone, 'nav-session', hello_sent=True)
            source.survey()
        self.assertEqual(client.post('/mode', json={'mode': mode}).status_code, 200)
        self.keep_fresh(phone, lambda: client.get('/health').json()['phone'] == 'ok')
        response = client.post('/arm')
        self.assertEqual(response.status_code, 200, response.text)

    def next_path(self, live, wanted, limit=400):
        for _ in range(limit):
            message = live.receive_json()
            if message['type'] == 'path' and wanted(message['points']):
                return message
        self.fail('missing path message')

    def test_goal_requires_arming_in_navigate_mode(self):
        with TestClient(self.app()) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            self.assertEqual(client.post('/goal', json={'x': 0., 'z': .5}).status_code, 409)
            for mode in ('manual', 'explore'):
                self.arm(client, phone, mode)
                self.assertEqual(client.post('/goal', json={'x': 0., 'z': .5}).status_code, 409)
            self.assertFalse(client.app.state.nav.kind == 'goal')

    def test_goal_publishes_its_path_drives_through_the_motion_pump_and_stop_clears_it(self):
        with TestClient(self.app()) as client, \
                client.websocket_connect('/live') as live, client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            self.arm(client, phone, 'navigate')
            response = client.post('/goal', json={'x': 0., 'z': .5})  # 0.5 m ahead on calibrated surveyed floor
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body['version'], 1)
            self.assertEqual(body['points'][-1], [0., .5])
            self.assertEqual(body['points'][0], [0., 0.])
            self.assertEqual(self.next_path(live, bool)['points'], body['points'])
            self.keep_fresh(phone, lambda: any(v > 0 for v, _ in self.sends()))
            self.assertEqual(client.app.state.nav.kind, 'goal')
            with client.websocket_connect('/live') as late:  # a new viewer gets the current path
                self.assertEqual(self.next_path(late, lambda p: True)['points'], body['points'])
            for v, w in self.sends():
                self.assertTrue(0 <= v <= .15 and abs(w) <= .5)
            health = client.post('/stop').json()
            stopped = len(self.car.calls)
            self.assertEqual((health['armed'], health['stop_reason']), (False, 'operator_stop'))
            self.next_path(live, lambda p: p == [])
            self.assertFalse(client.app.state.nav.active)
            self.assertEqual(self.car.calls[-1], ('zero',))
            self.stream(phone, .2)
            self.assertNotIn('send', [call[0] for call in self.car.calls[stopped:]])

    def test_navigation_has_no_drive_path_around_the_motion_pump(self):
        self.assertFalse(hasattr(backend.app, 'drive'))
        self.assertFalse(hasattr(backend.navigator, 'drive'))
        # The follower cruises at 0.15 m/s; a lower motion cap shows every command went through Motion.
        with TestClient(self.app(motion_limits=MotionLimits(max_speed_mps=.05))) as client, \
                client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            self.arm(client, phone, 'navigate')
            self.assertEqual(client.post('/goal', json={'x': 0., 'z': .5}).status_code, 200)
            self.keep_fresh(phone, lambda: len(self.sends()) >= 5)
            self.assertEqual({v for v, _ in self.sends() if v > 0}, {.05})

    def test_stop_and_rearm_while_planning_does_not_revive_the_goal(self):
        planning, release = threading.Event(), threading.Event()
        plan = backend.navigator.Navigator._plan

        def gated(nav, *args):
            planning.set()
            release.wait(5)
            return plan(nav, *args)
        with patch.object(backend.navigator.Navigator, '_plan', gated), TestClient(self.app()) as client, \
                client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            self.arm(client, phone, 'navigate')
            answer = []
            request = threading.Thread(target=lambda: answer.append(client.post('/goal', json={'x': 0., 'z': .5})))
            request.start()
            self.assertTrue(planning.wait(5))
            client.post('/stop')
            self.arm(client, phone, 'navigate')
            rearmed = len(self.car.calls)
            release.set()
            request.join(5)
            self.assertEqual((answer[0].status_code, answer[0].json()['detail']), (409, 'Stopped while planning'))
            self.stream(phone, .2)
            self.assertFalse(client.app.state.nav.active)
            self.assertTrue(client.get('/health').json()['armed'])
            self.assertEqual(self.car.calls[rearmed:], [])

    def test_unplannable_goal_disarms_with_the_reason(self):
        with TestClient(self.app()) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            self.arm(client, phone, 'navigate')
            response = client.post('/goal', json={'x': 500., 'z': 500.})  # beyond the padding cap
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()['detail'], 'out_of_bounds')
            health = client.get('/health').json()
            self.assertEqual((health['armed'], health['stop_reason']), (False, 'out_of_bounds'))
            self.assertFalse(client.app.state.nav.active)

    def test_stops_raised_elsewhere_end_the_run(self):
        triggers = [('mode_change', lambda client, phone: client.post('/mode', json={'mode': 'manual'})),
                    ('session_reset', lambda client, phone: client.post('/session')),
                    ('tracking_lost', lambda client, phone: phone.send_json(self.pose(tracking='limited'))),
                    ('phone_disconnected', None)]  # leaving the phone block closes it
        for reason, trigger in triggers:
            with self.subTest(reason=reason), TestClient(self.app()) as client:
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(self.hello())
                    self.arm(client, phone, 'navigate')
                    self.assertEqual(client.post('/goal', json={'x': 0., 'z': .5}).status_code, 200)
                    self.assertTrue(client.app.state.nav.active)
                    if trigger is not None:
                        trigger(client, phone)
                        self.wait_halted(client, reason)
                if trigger is None:
                    self.wait_halted(client, reason)

    def wait_halted(self, client, reason):
        deadline = time.monotonic() + 2
        while client.app.state.nav.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(client.app.state.nav.active)
        self.assertFalse(client.app.state.armed)
        # Read at once: with no more poses the watchdog later overwrites it with pose_stale.
        self.assertEqual(client.app.state.stop_reason, reason)
        self.assertEqual(client.app.state.nav.path, [])

    def test_explore_runs_with_a_calibrated_floor_and_stop_ends_it(self):
        with TestClient(self.app()) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            self.arm(client, phone, 'explore')
            self.keep_fresh(phone, lambda: len(self.sends()) >= 5)
            self.assertTrue(client.app.state.nav.active)
            client.post('/stop')
            self.assertFalse(client.app.state.nav.active)
            self.assertFalse(client.get('/health').json()['armed'])


if __name__ == '__main__':
    unittest.main()
