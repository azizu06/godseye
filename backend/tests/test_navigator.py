"""Goal/explore runner proof with a kinematic stand-in rover; the drive adapter only logs."""
import asyncio
import math
import time
import unittest
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.navigation import FollowerConfig, Grid, PlannerConfig, path_blocked, plan_path
from backend.navigator import Navigator, NavSettings, RoverPose
from backend.occupancy import CELL_M

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
        if self.cells is None:
            return self.revision, None, None
        return self.revision, (0., 0.), self.cells


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
    def __init__(self, rover, occupancy, mode='navigate', **settings):
        self.rover, self.occupancy = rover, occupancy
        self.stops, self.paths = [], []
        self.armed, self.mode = True, mode
        self.nav = Navigator(NavSettings(**{**FAST, **settings}), pose=rover.pose,
                             occupancy=lambda: self.occupancy, drive=rover.drive, stop=self.stop,
                             publish=self.publish, armed_mode=lambda: self.mode if self.armed else None)

    def stop(self, reason):
        """Mirrors backend.app stop(): disarm, halt the run, then a zero drive."""
        self.armed = False
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
        h.nav.start_goal(goal, initial)
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

    async def test_wall_seen_mid_run_blocks_the_path_and_the_run_replans_around_it(self):
        occupancy = FakeOccupancy(cells(UNKNOWN))
        start, goal = (.5, .5), (2.5, .5)
        initial = initial_plan(occupancy.cells, start, goal)  # straight through unknown

        def reveal_wall(rover):
            if rover.x > .9 and occupancy.revision == 1:
                occupancy.set(walled(occupancy.cells))
        # No periodic replan: only the blocked-path check on the map update can save the run.
        h = Harness(Rover(*start, math.pi / 2, on_move=reveal_wall), occupancy, replan_s=100.)
        h.nav.start_goal(goal, initial)
        await h.finished()
        self.assert_stopped(h, 'arrived')
        self.assertEqual(occupancy.revision, 2)
        grid = Grid.from_array(occupancy.cells, origin=(0., 0.), cell_m=CELL_M)
        self.assertTrue(path_blocked(grid, initial.points))
        detour = h.paths[1]
        self.assertFalse(path_blocked(grid, detour))
        self.assertGreater(max(z for _, z in detour), 2.2)
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
        h.nav.start_goal(goal, initial)
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
        h.nav.start_goal(goal, initial)
        await h.finished()
        self.assert_stopped(h, 'destination_blocked')

    async def test_replan_hitting_the_search_limit_stops(self):
        occupancy = FakeOccupancy(walled(cells()))
        start, goal = (.5, .5), (2.5, .5)
        initial = initial_plan(occupancy.cells, start, goal)
        h = Harness(Rover(*start, math.pi / 2), occupancy, planner=PlannerConfig(max_expansions=20))
        h.nav.start_goal(goal, initial)
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
                h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)))
                await h.finished()
                self.assert_stopped(h, reason)
                self.assertEqual(set(rover.commands), {(0., 0.)})

    async def test_pose_going_stale_mid_run_stops(self):
        occupancy = FakeOccupancy(cells())
        rover = Rover(.5, .5, math.pi / 2)
        h = Harness(rover, occupancy)
        h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)))
        await wait_until(lambda: any(v > 0 for v, _ in rover.commands))
        rover.age_s = .3
        await h.finished()
        self.assert_stopped(h, 'pose_stale')

    async def test_commanded_motion_without_pose_change_trips_the_no_progress_watchdog(self):
        occupancy = FakeOccupancy(cells())
        rover = Rover(.5, .5, math.pi / 2, moves=False)  # the logging-only car never moves
        h = Harness(rover, occupancy, no_progress_s=.2)
        h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)))
        await h.finished()
        self.assert_stopped(h, 'no_progress')
        self.assertTrue(any(v > 0 for v, _ in rover.commands[:-1]))

    async def test_external_stop_ends_driving_immediately_and_clears_the_path(self):
        occupancy = FakeOccupancy(cells())
        rover = Rover(.5, .5, math.pi / 2)
        h = Harness(rover, occupancy)
        h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)))
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
            h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)))
            await wait_until(lambda: len(h.rover.commands) > 2)
            spoil(h)
            await h.finished()
            self.assert_stopped(h, 'disarmed')

    async def test_planning_errors_stop_with_nav_error(self):
        occupancy = FakeOccupancy(cells())
        h = Harness(Rover(.5, .5, math.pi / 2), occupancy)
        h.nav.start_goal((2.5, .5), initial_plan(occupancy.cells, (.5, .5), (2.5, .5)))
        occupancy.fail = True
        with self.assertLogs('backend.navigator', level='ERROR'):
            await h.finished()
        self.assert_stopped(h, 'nav_error')

    async def test_unchanged_replans_publish_the_path_once(self):
        occupancy = FakeOccupancy(cells())
        rover = Rover(.5, .5, math.pi / 2, moves=False)
        h = Harness(rover, occupancy, no_progress_s=100.)
        initial = initial_plan(occupancy.cells, (.5, .5), (2.5, .5))
        h.nav.start_goal((2.5, .5), initial)
        await asyncio.sleep(.3)  # about six replans from the same pose
        self.assertEqual(h.paths, [initial.points])
        self.assertEqual(h.nav.path, initial.points)
        await h.nav.aclose()
        self.assertFalse(h.nav.active)
        self.assertEqual(h.paths, [initial.points, []])

    async def test_explore_waits_for_a_floor_without_moving_and_survives_map_updates(self):
        occupancy = FakeOccupancy(None)  # no floor estimated yet
        rover = Rover(1.5, .75, 0.)
        h = Harness(rover, occupancy, mode='explore')
        h.nav.start_explore()
        for _ in range(5):
            await asyncio.sleep(.03)
            occupancy.set(None)  # new evidence, still no floor
        self.assertTrue(h.nav.active)
        self.assertEqual(h.stops, [])
        self.assertEqual(set(rover.commands), {(0., 0.)})
        corridor = cells(UNKNOWN)
        corridor[5:25, 5:55] = FREE
        occupancy.set(corridor)
        await wait_until(lambda: h.paths and any(v > 0 for v, _ in rover.commands))
        await h.nav.aclose()

    async def test_explore_visits_frontiers_and_stops_when_the_map_is_closed(self):
        corridor = cells(UNKNOWN)
        corridor[5:25, 5:55] = FREE  # explored strip; everything around it is unknown
        occupancy = FakeOccupancy(corridor)
        rover = Rover(1.5, .75, 0.)
        h = Harness(rover, occupancy, mode='explore')
        h.nav.start_explore()
        # Two distinct path ends: it reached one frontier and moved on to the next.
        await wait_until(lambda: len({tuple(p[-1]) for p in h.paths if p}) >= 2)
        grid = Grid.from_array(corridor, origin=(0., 0.), cell_m=CELL_M)
        for points in h.paths:
            if points:
                row, col = grid.world_to_cell(*points[-1])
                self.assertEqual(corridor[row, col], FREE)
        occupancy.set(enclosed())
        await h.finished()
        self.assert_stopped(h, 'explore_complete')
        self.assert_within_limits(rover.commands)


class LiveNavigationTests(unittest.TestCase):
    """/goal, path publishing and app-level stops. Arming is impossible until the car
    reports ok, so these tests set the armed flag directly to stand in for that."""

    def setUp(self):
        self.t_capture = 0.

    def hello(self):
        return dict(version=1, type='hello', device='test-phone', session_id='nav-session',
                    map_epoch=1, supports_scene_depth=True, supports_mesh=True)

    def pose(self, tracking='normal'):
        # Identity rotation: camera forward is world -Z, so yaw is pi; position (2, 3, 4).
        self.t_capture += .02
        return dict(version=1, type='pose', session_id='nav-session', map_epoch=1, frame_id=1,
                    t_capture=self.t_capture, t_wall_ms=int(time.time() * 1000),
                    transform=[1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 2., 3., 4., 1.],
                    tracking=tracking)

    def keep_fresh(self, phone, condition, timeout=3.):
        """Stream poses (so no watchdog fires) until condition() holds."""
        deadline = time.monotonic() + timeout
        while True:
            phone.send_json(self.pose())
            if condition():
                return
            if time.monotonic() > deadline:
                self.fail('timed out')
            time.sleep(.02)

    def arm(self, client, phone, mode):
        self.assertEqual(client.post('/mode', json={'mode': mode}).status_code, 200)
        self.keep_fresh(phone, lambda: client.get('/health').json()['phone'] == 'ok')
        client.app.state.armed, client.app.state.stop_reason = True, None

    def next_path(self, live, wanted, limit=400):
        for _ in range(limit):
            message = live.receive_json()
            if message['type'] == 'path' and wanted(message['points']):
                return message
        self.fail('missing path message')

    def test_goal_requires_arming_in_navigate_mode(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            self.assertEqual(client.post('/goal', json={'x': 2., 'z': 3.}).status_code, 409)
            for mode in ('manual', 'explore'):
                self.arm(client, phone, mode)
                self.assertEqual(client.post('/goal', json={'x': 2., 'z': 3.}).status_code, 409)
            self.assertFalse(client.app.state.nav.kind == 'goal')

    def test_goal_publishes_its_path_drives_the_logging_stub_and_stop_clears_it(self):
        with patch('backend.app.drive') as drive, TestClient(create_app(':memory:')) as client, \
                client.websocket_connect('/live') as live, client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            self.arm(client, phone, 'navigate')
            response = client.post('/goal', json={'x': 2., 'z': 3.})  # 1 m straight ahead, unmapped
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body['version'], 1)
            self.assertEqual(body['points'][-1], [2., 3.])
            self.assertEqual(body['points'][0], [2., 4.])
            self.assertEqual(self.next_path(live, bool)['points'], body['points'])
            self.keep_fresh(phone, lambda: any(c.args[0] > 0 for c in drive.call_args_list))
            self.assertEqual(client.app.state.nav.kind, 'goal')
            with client.websocket_connect('/live') as late:  # a new viewer gets the current path
                self.assertEqual(self.next_path(late, lambda p: True)['points'], body['points'])
            for call in drive.call_args_list:
                v, w = call.args
                self.assertTrue(0 <= v <= .2 and abs(w) <= .5)
            health = client.post('/stop').json()
            self.assertEqual((health['armed'], health['stop_reason']), (False, 'operator_stop'))
            self.next_path(live, lambda p: p == [])
            self.assertFalse(client.app.state.nav.active)
            self.assertEqual(drive.call_args_list[-1].args, (0., 0.))

    def test_unplannable_goal_disarms_with_the_reason(self):
        with TestClient(create_app(':memory:')) as client, client.websocket_connect('/phone') as phone:
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
            with self.subTest(reason=reason), TestClient(create_app(':memory:')) as client:
                with client.websocket_connect('/phone') as phone:
                    phone.send_json(self.hello())
                    self.arm(client, phone, 'navigate')
                    self.assertEqual(client.post('/goal', json={'x': 2., 'z': 3.}).status_code, 200)
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

    def test_explore_runs_while_armed_in_explore_mode_and_stop_ends_it(self):
        with patch('backend.app.drive') as drive, TestClient(create_app(':memory:')) as client, \
                client.websocket_connect('/phone') as phone:
            phone.send_json(self.hello())
            self.arm(client, phone, 'explore')
            self.keep_fresh(phone, lambda: client.app.state.nav.kind == 'explore')
            self.keep_fresh(phone, lambda: drive.call_count >= 5)
            # No frames means no floor yet: explore waits in place with zero drive.
            self.assertEqual({call.args for call in drive.call_args_list}, {(0., 0.)})
            client.post('/stop')
            self.assertFalse(client.app.state.nav.active)
            self.assertFalse(client.get('/health').json()['armed'])


if __name__ == '__main__':
    unittest.main()
