"""Pure grid planning and pure-pursuit proof; no sockets, timing, or hardware."""
import base64
import math
import time
import unittest

import numpy as np

from backend.navigation import (FollowerConfig, Grid, PlannerConfig, PurePursuit, is_frontier,
                                nearest_frontier, path_blocked, path_message, plan_path, traversable_mask)

UNKNOWN, FREE, OCCUPIED = 0, 1, 2


def grid(width=60, height=60, fill=FREE, origin=(0., 0.), cell_m=.05):
    return Grid.from_array(np.full((height, width), fill, dtype=np.uint8), origin=origin, cell_m=cell_m)


def with_cells(base, assign):
    cells = base.cells.copy()
    assign(cells)
    return Grid.from_array(cells, origin=base.origin, cell_m=base.cell_m)


def yaw_from_transform(transform):
    """The backend's live pose yaw, copied from backend/app.py so a drift fails here."""
    return math.atan2(-transform[8], -transform[10])


def camera_transform(forward_xz):
    """Column-major camera-to-world transform whose camera forward (-Z column) is forward_xz."""
    fx, fz = forward_xz
    norm = math.hypot(fx, fz)
    fx, fz = fx / norm, fz / norm
    # Columns: camera +X, +Y (world up), +Z (= -forward), translation.
    z_col = (-fx, 0., -fz)
    y_col = (0., 1., 0.)
    x_col = (y_col[1] * z_col[2] - y_col[2] * z_col[1], y_col[2] * z_col[0] - y_col[0] * z_col[2],
             y_col[0] * z_col[1] - y_col[1] * z_col[0])
    return [*x_col, 0., *y_col, 0., *z_col, 0., 0., 0., 0., 1.]


def point_distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def min_clearance(points, occupied_centers, step=.01):
    best = math.inf
    for a, b in zip(points, points[1:]):
        n = max(1, int(point_distance(a, b) / step))
        for k in range(n + 1):
            t = k / n
            p = np.array([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
            best = min(best, float(np.min(np.hypot(*(occupied_centers - p).T))))
    return best


def occupied_centers(g):
    rows, cols = np.nonzero(g.cells == OCCUPIED)
    return np.stack([g.origin[0] + (cols + .5) * g.cell_m, g.origin[1] + (rows + .5) * g.cell_m], axis=1)


class GridTests(unittest.TestCase):
    def test_message_decodes_row_major_with_rows_along_z_like_the_dashboard(self):
        # 3 wide x 2 high; index i -> column i % width (x), row i // width (z).
        raw = bytes([0, 1, 2, 2, 1, 0])
        g = Grid.from_message(dict(version=1, type='occupancy', origin=[-1., 2.], cell_m=.5, width=3,
                                   height=2, cells=base64.b64encode(raw).decode()))
        self.assertEqual(g.cells.shape, (2, 3))
        self.assertEqual(g.cells[0].tolist(), [0, 1, 2])
        self.assertEqual(g.cells[1].tolist(), [2, 1, 0])
        # origin is the min-x/min-z corner of cell (0, 0), matching the dashboard's SVG rects.
        self.assertEqual(g.cell_center(0, 0), (-.75, 2.25))
        self.assertEqual(g.cell_center(1, 2), (.25, 2.75))  # row 1 (z), column 2 (x)
        self.assertEqual(g.world_to_cell(.25, 2.75), (1, 2))
        self.assertIsNone(g.world_to_cell(-1.01, 2.1))
        self.assertIsNone(g.world_to_cell(0., 3.0))  # z edge is exclusive

    def test_rejects_malformed_grids(self):
        cells = base64.b64encode(bytes(5)).decode()
        with self.assertRaises(ValueError):
            Grid.from_message(dict(origin=[0, 0], cell_m=.05, width=3, height=2, cells=cells))
        with self.assertRaises(ValueError):
            Grid.from_array(np.full((2, 2), 3, dtype=np.uint8), origin=(0, 0), cell_m=.05)
        with self.assertRaises(ValueError):
            Grid.from_array(np.zeros((2, 2), dtype=np.uint8), origin=(0, 0), cell_m=0)


class InflationTests(unittest.TestCase):
    def test_occupied_cells_are_inflated_by_radius_plus_margin(self):
        g = with_cells(grid(41, 41), lambda c: c.__setitem__((20, 20), OCCUPIED))
        mask = traversable_mask(g, PlannerConfig(robot_radius_m=.15, margin_m=.05))
        self.assertFalse(mask[20, 20])
        self.assertFalse(mask[20, 24])  # 0.20 m away, on the boundary
        self.assertTrue(mask[20, 25])  # 0.25 m away
        self.assertFalse(mask[23, 22])  # ~0.18 m diagonal
        self.assertTrue(mask[24, 24])  # ~0.28 m diagonal

    def test_unknown_is_traversable_by_default_and_can_be_blocked(self):
        g = with_cells(grid(10, 10), lambda c: c.__setitem__((5, 5), UNKNOWN))
        self.assertTrue(traversable_mask(g, PlannerConfig())[5, 5])
        self.assertFalse(traversable_mask(g, PlannerConfig(unknown_traversable=False))[5, 5])


class PlannerTests(unittest.TestCase):
    def test_straight_path_in_empty_grid(self):
        result = plan_path(grid(), (.3, .5), (2.7, .5))
        self.assertTrue(result.ok, result.reason)
        self.assertIsNone(result.reason)
        self.assertAlmostEqual(result.points[0][0], .3, delta=.03)
        self.assertAlmostEqual(result.points[-1][0], 2.7, delta=.03)
        for x, z in result.points:
            self.assertAlmostEqual(z, .5, delta=.03)
        steps = [point_distance(a, b) for a, b in zip(result.points, result.points[1:])]
        self.assertLessEqual(max(steps), .3 + 1e-9)
        length = sum(steps)
        self.assertAlmostEqual(length, 2.4, delta=.06)

    def test_routes_around_a_wall_with_clearance(self):
        # Wall across x = 1.5 from z = 0 to z = 2.2, leaving a gap near z = 3.
        def wall(c):
            c[0:44, 29:31] = OCCUPIED
        g = with_cells(grid(), wall)
        config = PlannerConfig(robot_radius_m=.15, margin_m=.03)
        result = plan_path(g, (.5, .5), (2.5, .5), config)
        self.assertTrue(result.ok, result.reason)
        self.assertGreater(max(z for _, z in result.points), 2.2)
        clearance = min_clearance(result.points, occupied_centers(g))
        # Cell centers of occupied cells are 0.025 m inside the wall face; the planner keeps
        # every waypoint and segment at least radius + margin from them (minus half a cell).
        self.assertGreaterEqual(clearance, .15 + .03 - .025)

    def test_no_path_when_goal_is_enclosed(self):
        def box(c):
            c[20:41, 20] = OCCUPIED
            c[20:41, 40] = OCCUPIED
            c[20, 20:41] = OCCUPIED
            c[40, 20:41] = OCCUPIED
        result = plan_path(with_cells(grid(), box), (.3, .3), (1.5, 1.5))
        self.assertFalse(result.ok)
        outside_unknown = with_cells(grid(fill=UNKNOWN), box)
        self.assertEqual(plan_path(outside_unknown, (.3, .3), (1.5, 1.5)).reason, 'no_path')
        self.assertEqual(result.reason, 'no_path')
        self.assertEqual(result.points, [])

    def test_goal_reasons(self):
        def blocks(c):
            c[40:60, 40:60] = OCCUPIED
            c[0:20, 40:60] = UNKNOWN
        g = with_cells(grid(), blocks)
        strict = PlannerConfig(unknown_traversable=False)
        self.assertEqual(plan_path(g, (.5, .5), (2.5, 2.5)).reason, 'goal_occupied')
        self.assertEqual(plan_path(g, (.5, .5), (2.5, 2.5), strict).reason, 'goal_occupied')
        self.assertIsNone(plan_path(g, (.5, .5), (2.5, .5)).reason)  # unknown goal is allowed
        self.assertEqual(plan_path(g, (.5, .5), (2.5, .5), strict).reason, 'goal_unknown')
        self.assertEqual(plan_path(g, (.5, .5), (9., .5), strict).reason, 'out_of_bounds')
        self.assertEqual(plan_path(g, (-1., .5), (1., .5), strict).reason, 'out_of_bounds')
        self.assertEqual(plan_path(g, (.5, .5), (500., 500.)).reason, 'out_of_bounds')  # beyond the pad cap
        # A free goal inside the inflation band is still unreachable for the rover body.
        self.assertEqual(plan_path(g, (.5, .5), (1.9, 2.5), PlannerConfig(snap_radius_m=0.)).reason,
                         'goal_occupied')

    def test_goal_and_start_beyond_the_map_plan_through_padded_unknown(self):
        result = plan_path(grid(), (-1., .5), (5., 1.))
        self.assertTrue(result.ok, result.reason)
        self.assertAlmostEqual(result.points[0][0], -1., delta=1e-9)
        self.assertEqual(result.points[-1], [5., 1.])

    def test_prefers_known_free_floor_but_crosses_unknown_when_needed(self):
        # Unknown band across the middle, open only near z >= 2.5 on known-free floor.
        def band(c):
            c[0:50, 25:35] = UNKNOWN
        g = with_cells(grid(), band)
        detour = plan_path(g, (.5, 1.), (2.5, 1.))
        self.assertTrue(detour.ok, detour.reason)
        self.assertLess(max(z for _, z in detour.points), 2.5)  # 3x unknown still beats the detour here
        cheap = plan_path(g, (.5, .5), (2.5, .5), PlannerConfig(unknown_cost=30.))
        self.assertGreater(max(z for _, z in cheap.points), 2.4)  # expensive unknown: go around
        full = with_cells(grid(), lambda c: c.__setitem__((slice(None), slice(25, 35)), UNKNOWN))
        through = plan_path(full, (.5, .5), (2.5, .5), PlannerConfig(unknown_cost=30.))
        self.assertTrue(through.ok, through.reason)
        self.assertLess(max(z for _, z in through.points), .6)

    def test_path_blocked_detects_a_new_wall_and_replan_routes_around(self):
        unknown_room = grid(fill=UNKNOWN)
        first = plan_path(unknown_room, (.5, .5), (2.5, .5))
        self.assertTrue(first.ok, first.reason)
        self.assertFalse(path_blocked(unknown_room, first.points))
        # New depth reveals a wall across the route, open near z >= 2.2.
        walled = with_cells(unknown_room, lambda c: c.__setitem__((slice(0, 44), slice(29, 31)), OCCUPIED))
        self.assertTrue(path_blocked(walled, first.points))
        second = plan_path(walled, (.5, .5), (2.5, .5))
        self.assertTrue(second.ok, second.reason)
        self.assertFalse(path_blocked(walled, second.points))
        self.assertGreater(max(z for _, z in second.points), 2.2)
        # Only the remaining legs count: past the wall the old path is clear again.
        past = next(i for i, (x, _) in enumerate(first.points) if x > 2.)
        self.assertFalse(path_blocked(walled, first.points, start_index=past))
        sealed = with_cells(walled, lambda c: c.__setitem__((slice(None), slice(29, 31)), OCCUPIED))
        self.assertEqual(plan_path(sealed, (.5, .5), (2.5, .5)).reason, 'no_path')

    def test_fresh_plans_are_never_reported_blocked(self):
        rng = np.random.default_rng(7)
        for seed in range(12):
            cells = np.where(rng.random((60, 60)) < .01, OCCUPIED,
                             np.where(rng.random((60, 60)) < .3, UNKNOWN, FREE)).astype(np.uint8)
            g = Grid.from_array(cells, origin=(-1.3, .7), cell_m=.05)
            for config in (PlannerConfig(), PlannerConfig(unknown_traversable=False)):
                result = plan_path(g, (-1.1 + rng.random() * .4, .9 + rng.random() * .4),
                                   (1.1 + rng.random() * .4, 3.1 + rng.random() * .4), config)
                if result.ok:
                    self.assertFalse(path_blocked(g, result.points, config), (seed, config))

    def test_goal_snaps_to_nearby_traversable_cell(self):
        g = with_cells(grid(), lambda c: c.__setitem__((10, 40), OCCUPIED))
        config = PlannerConfig(robot_radius_m=.05, margin_m=0., snap_radius_m=.15)
        result = plan_path(g, (.5, .5), (2.025, .525), config)
        self.assertTrue(result.ok, result.reason)
        self.assertLessEqual(point_distance(result.points[-1], (2.025, .525)), .15)
        self.assertGreater(point_distance(result.points[-1], (2.025, .525)), .02)

    def test_start_in_unknown_under_rover_snaps_to_free(self):
        g = with_cells(grid(), lambda c: c.__setitem__((slice(8, 12), slice(8, 12)), UNKNOWN))
        result = plan_path(g, (.5, .5), (2.5, .5))
        self.assertTrue(result.ok, result.reason)

    def test_search_is_bounded(self):
        rng = np.random.default_rng(3)
        cells = np.where(rng.random((200, 200)) < .02, OCCUPIED, FREE).astype(np.uint8)
        cells[95:105, 95:105] = FREE
        g = Grid.from_array(cells, origin=(0., 0.), cell_m=.05)
        config = PlannerConfig(robot_radius_m=.0, margin_m=.0, max_expansions=50)
        result = plan_path(g, (.2, .2), (9.8, 9.8), config)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'search_limit')
        self.assertLessEqual(result.expansions, 50)

    def test_no_corner_cutting_between_diagonal_obstacles(self):
        # Two occupied cells touch only at a corner; the only diagonal gap is illegal.
        def cells(c):
            c[:, 5] = OCCUPIED
            c[4, 5] = FREE
            c[5, 4] = OCCUPIED
            c[3, 6] = OCCUPIED
            c[4, 6] = OCCUPIED
            c[5, 5] = OCCUPIED
        g = with_cells(grid(10, 10, cell_m=1.), cells)
        # Opening at (row 4, col 5): entering from (4,4) and leaving to (5,6)/(3,6) diagonally
        # would cut corners; the straight exit (4,6) is blocked, so no path exists.
        config = PlannerConfig(robot_radius_m=0., margin_m=0., snap_radius_m=0.)
        result = plan_path(g, (1.5, 4.5), (8.5, 4.5), config)
        self.assertEqual(result.reason, 'no_path')

    def test_path_message_matches_contract(self):
        result = plan_path(grid(), (.3, .5), (1.3, .5))
        message = path_message(result.points)
        self.assertEqual(message['version'], 1)
        self.assertEqual(message['type'], 'path')
        self.assertTrue(all(len(p) == 2 and all(isinstance(v, float) for v in p) for p in message['points']))
        self.assertEqual(path_message([], session_id='s', map_epoch=2),
                         dict(version=1, type='path', session_id='s', map_epoch=2, points=[]))

    def test_plans_on_a_400_by_400_grid_quickly(self):
        def best_ms(cells, start, goal):
            g = Grid.from_array(cells, origin=(0., 0.), cell_m=.05)
            timings = []
            for _ in range(3):
                began = time.perf_counter()
                result = plan_path(g, start, goal)
                timings.append(time.perf_counter() - began)
                self.assertTrue(result.ok, result.reason)
            return min(timings) * 1000, result

        room = np.full((400, 400), FREE, dtype=np.uint8)
        room[0, :] = room[-1, :] = room[:, 0] = room[:, -1] = OCCUPIED
        for r, c, h, w in [(50, 50, 40, 80), (200, 100, 30, 30), (120, 250, 80, 20), (300, 300, 40, 60),
                           (250, 40, 20, 120)]:
            room[r:r + h, c:c + w] = OCCUPIED  # furniture
        room[180:200, 0:300] = OCCUPIED  # partial wall
        speckled = room.copy()
        speckled[(speckled == FREE) & (np.random.default_rng(1).random(room.shape) < .4)] = UNKNOWN
        slalom = np.full((400, 400), FREE, dtype=np.uint8)
        for k, col in enumerate(range(60, 400, 70)):
            slalom[(40 if k % 2 else 0):(400 if k % 2 else 360), col:col + 2] = OCCUPIED
        cases = [('furnished 20 m room', room, (1., 1.), (19., 19.), .2),
                 ('furnished room, 40% unknown', speckled, (1., 1.), (19., 19.), .2),
                 ('all unknown, corner to corner', np.zeros((400, 400), dtype=np.uint8), (.5, .5), (19.5, 19.5), .2),
                 ('adversarial slalom (full flood)', slalom, (1., 1.), (19., 19.), 1.)]
        for name, cells, start, goal, limit_s in cases:
            ms, result = best_ms(cells, start, goal)
            print(f'\n[bench] 400x400 {name}: {ms:.1f} ms, {result.expansions} expansions, '
                  f'{len(result.points)} waypoints', end='')
            self.assertLess(ms, limit_s * 1000, name)
        print()


def simulate(follower, pose, dt=.05, steps=2000):
    """Integrate the backend yaw convention: heading (sin yaw, cos yaw), yaw += yaw_rate * dt."""
    x, z, yaw = pose
    trace = []
    for _ in range(steps):
        command = follower.step(x, z, yaw)
        trace.append(command)
        if command.arrived:
            break
        yaw += command.yaw_rate_rps * dt
        x += command.v_mps * math.sin(yaw) * dt
        z += command.v_mps * math.cos(yaw) * dt
    return (x, z, yaw), trace


class FollowerTests(unittest.TestCase):
    def test_backend_yaw_convention(self):
        # Camera forward (-Z column) along world +Z is yaw 0; along +X is +pi/2.
        self.assertAlmostEqual(yaw_from_transform(camera_transform((0., 1.))), 0.)
        self.assertAlmostEqual(yaw_from_transform(camera_transform((1., 0.))), math.pi / 2)
        self.assertAlmostEqual(abs(yaw_from_transform(camera_transform((0., -1.)))), math.pi)
        # With +Y up and forward +Z, the rover's left is up x forward = +X, so +yaw is a left
        # (counterclockwise from above) turn, and so is a positive yaw_rate_rps.
        left = np.cross([0., 1., 0.], [0., 0., 1.])
        self.assertEqual(left.tolist(), [1., 0., 0.])

    def test_target_ahead_needs_no_turn(self):
        follower = PurePursuit([[0., 0.], [0., 2.]])
        command = follower.step(0., 0., yaw_from_transform(camera_transform((0., 1.))))
        self.assertAlmostEqual(command.yaw_rate_rps, 0., places=6)
        self.assertGreater(command.v_mps, 0.)
        self.assertFalse(command.arrived)

    def test_target_on_plus_x_side_turns_toward_it(self):
        follower = PurePursuit([[0., 0.], [.3, .3], [1., 1.]])
        command = follower.step(0., 0., 0.)
        self.assertGreater(command.yaw_rate_rps, 0.)  # +X is left of a +Z-facing rover
        right = PurePursuit([[0., 0.], [-.3, .3], [-1., 1.]]).step(0., 0., 0.)
        self.assertLess(right.yaw_rate_rps, 0.)

    def test_large_heading_error_rotates_in_place(self):
        command = PurePursuit([[0., 0.], [0., -1.]]).step(0., 0., 0.)
        self.assertEqual(command.v_mps, 0.)
        self.assertAlmostEqual(abs(command.yaw_rate_rps), .5)
        side = PurePursuit([[0., 0.], [1., 0.]]).step(0., 0., 0.)
        self.assertEqual(side.v_mps, 0.)
        self.assertAlmostEqual(side.yaw_rate_rps, .5)

    def test_outputs_are_clamped_to_contract_limits(self):
        follower = PurePursuit([[0., 0.], [0., 5.]], FollowerConfig(cruise_mps=5., max_yaw_rate_rps=9.))
        command = follower.step(0., 0., 0.)
        self.assertLessEqual(command.v_mps, .20)
        spin = PurePursuit([[0., 0.], [0., -1.]], FollowerConfig(max_yaw_rate_rps=9.)).step(0., 0., 0.)
        self.assertLessEqual(abs(spin.yaw_rate_rps), .5)
        for (x, z, yaw) in [(0., 0., .4), (.1, .2, -.3), (0., 1., 2.)]:
            c = PurePursuit([[0., 0.], [.5, 1.], [-1., 2.]]).step(x, z, yaw)
            self.assertTrue(0. <= c.v_mps <= .15 + 1e-9)
            self.assertLessEqual(abs(c.yaw_rate_rps), .5 + 1e-9)

    def test_slows_near_goal_and_reports_arrival(self):
        follower = PurePursuit([[0., 0.], [0., 2.]])
        far = follower.step(0., 0., 0.)
        near = PurePursuit([[0., 0.], [0., 2.]]).step(0., 1.75, 0.)
        self.assertLess(near.v_mps, far.v_mps)
        self.assertGreater(near.v_mps, 0.)
        done = PurePursuit([[0., 0.], [0., 2.]]).step(.05, 1.9, .3)
        self.assertTrue(done.arrived)
        self.assertEqual((done.v_mps, done.yaw_rate_rps), (0., 0.))

    def test_closed_loop_follows_planned_path_to_goal(self):
        def wall(c):
            c[0:44, 29:31] = OCCUPIED
        g = with_cells(grid(), wall)
        result = plan_path(g, (.5, .5), (2.5, .5))
        self.assertTrue(result.ok, result.reason)
        follower = PurePursuit(result.points)
        (x, z, _), trace = simulate(follower, (.5, .5, math.pi / 2))
        self.assertTrue(trace[-1].arrived)
        self.assertLessEqual(point_distance((x, z), (2.5, .5)), .15)
        self.assertTrue(all(0 <= c.v_mps <= .15 and abs(c.yaw_rate_rps) <= .5 for c in trace))

    def test_optional_scan_turn_rotates_once_in_place_before_following(self):
        path = [[0., 0.], [0., 2.]]
        self.assertEqual(PurePursuit(path).step(0., 0., 0.).status, 'follow')  # off by default
        follower = PurePursuit(path, FollowerConfig(scan_turn=True))
        (x, z, _), trace = simulate(follower, (0., 0., 0.))
        scanning = [c for c in trace if c.status == 'scan']
        self.assertTrue(all(c.v_mps == 0. and c.yaw_rate_rps == .5 for c in scanning))
        self.assertEqual(trace[0].status, 'scan')
        # 2*pi at 0.5 rad/s with 0.05 s steps is about 252 steps.
        self.assertAlmostEqual(len(scanning), 2 * math.pi / (.5 * .05), delta=3)
        self.assertNotIn('scan', [c.status for c in trace[len(scanning):]])
        self.assertTrue(trace[-1].arrived)

    def test_empty_path_stops(self):
        command = PurePursuit([]).step(0., 0., 0.)
        self.assertEqual((command.v_mps, command.yaw_rate_rps), (0., 0.))
        self.assertFalse(command.arrived)
        self.assertEqual(command.status, 'empty')


class FrontierTests(unittest.TestCase):
    def test_finds_nearest_reachable_frontier(self):
        def cells(c):
            c[:, :] = UNKNOWN
            c[5:25, 5:55] = FREE  # explored corridor; the rest is unknown
            c[5:25, 30] = OCCUPIED  # wall cutting the corridor
        g = with_cells(grid(), cells)
        goal = nearest_frontier(g, (.5, .75), PlannerConfig(robot_radius_m=.05, margin_m=0.))
        self.assertIsNotNone(goal)
        row, col = g.world_to_cell(*goal)
        self.assertEqual(g.cells[row, col], FREE)
        neighbors = g.cells[max(row - 1, 0):row + 2, max(col - 1, 0):col + 2]
        self.assertIn(UNKNOWN, neighbors)
        self.assertLess(col, 30)  # never across the wall
        self.assertGreaterEqual(point_distance(goal, (.5, .75)), PlannerConfig().frontier_min_distance_m)

    def test_free_cells_on_the_cropped_grid_edge_are_frontiers(self):
        g = grid(20, 20)  # all free: the edge borders unmapped space
        goal = nearest_frontier(g, (.5, .5), PlannerConfig(robot_radius_m=.05, margin_m=0.))
        self.assertIsNotNone(goal)
        self.assertTrue(is_frontier(g, goal))
        self.assertFalse(is_frontier(g, (.5, .5)))
        self.assertFalse(is_frontier(g, (5., 5.)))  # outside the grid

    def test_replaced_follower_keeps_scan_progress(self):
        follower = PurePursuit([[0., 0.], [0., 2.]], FollowerConfig(scan_turn=True))
        follower.step(0., 0., 0.)
        follower.step(0., 0., 3.)
        follower.step(0., 0., 6.)  # about 6 rad turned so far
        replacement = follower.replaced([[0., 0.], [0., 3.]])
        self.assertEqual(replacement.step(0., 0., .3).status, 'follow')

    def test_no_frontier_when_fully_explored(self):
        g = with_cells(grid(20, 20), lambda c: (c.__setitem__((0, slice(None)), OCCUPIED),
                                                 c.__setitem__((-1, slice(None)), OCCUPIED),
                                                 c.__setitem__((slice(None), 0), OCCUPIED),
                                                 c.__setitem__((slice(None), -1), OCCUPIED)))
        self.assertIsNone(nearest_frontier(g, (.5, .5), PlannerConfig(robot_radius_m=.05, margin_m=0.)))


if __name__ == '__main__':
    unittest.main()
