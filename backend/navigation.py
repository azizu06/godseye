"""Pure 2D navigation: grid A*, path shortcutting, pure pursuit and frontier goals.

No FastAPI, timing, I/O or hardware. Everything here is a function of its inputs.

Grid layout (mirrors the dashboard, dashboard/src/Scene.tsx top-down map and
dashboard/src/protocol.ts ``decodeCells``): the contract ``occupancy`` message's
``cells`` decode to ``width * height`` bytes in row-major order. Byte ``i`` is
column ``i % width`` along world +X and row ``i // width`` along world +Z.
``origin`` is the world (x, z) of the min-x / min-z corner of cell (0, 0), so a
cell's center is ``(origin_x + (col + .5) * cell_m, origin_z + (row + .5) * cell_m)``.
Here ``Grid.cells[row, col]`` holds that byte: 0 unknown, 1 free, 2 occupied.

Yaw convention (backend/app.py live pose): ``yaw_rad = atan2(-t[8], -t[10])`` with
``t`` the column-major camera-to-world transform, i.e. the camera forward (-Z
column) projected on the floor. yaw 0 faces world +Z and yaw +pi/2 faces +X, so
the heading vector is ``(sin yaw, cos yaw)`` in (x, z). With +Y up, a rover facing
+Z has +X on its left, so increasing yaw is a left (counterclockwise seen from
above) turn. ``yaw_rate_rps`` here is d(yaw_rad)/dt: positive turns left. This is
the same sign the dashboard steering and simulator use.

Unknown space policy: the map is built while driving and depth only sees a few
meters ahead, so by default unknown cells are traversable at ``unknown_cost``
times the cost of free cells (known-free routes win unless the detour is long),
and the grid is virtually padded with unknown so a goal or rover outside the
current map extent can still be planned for. Occupied cells, inflated by the
rover radius plus a margin, are always blocked. ``unknown_traversable=False``
restores the conservative policy. The caller is expected to call
``path_blocked`` on every new grid and replan when it returns True.

A* and pure pursuit follow the textbook formulations (Hart, Nilsson and Raphael
1968; Coulter 1992), written here from scratch rather than copied.
"""
from __future__ import annotations

import base64
import dataclasses
import heapq
import math
from dataclasses import dataclass, field

import numpy as np

UNKNOWN, FREE, OCCUPIED = 0, 1, 2
MAX_SPEED_MPS = .20  # contract section 4 hard maximum
MAX_YAW_RATE_RPS = .5  # contract section 4 hard maximum
_SQRT2 = math.sqrt(2.)


@dataclass(frozen=True)
class Grid:
    origin: tuple[float, float]  # world (x, z) of the min corner of cell (0, 0)
    cell_m: float
    cells: np.ndarray  # (height, width) uint8, read-only; cells[row (z), col (x)]

    @property
    def width(self) -> int:
        return self.cells.shape[1]

    @property
    def height(self) -> int:
        return self.cells.shape[0]

    @classmethod
    def from_array(cls, cells, *, origin, cell_m) -> Grid:
        array = np.array(cells, dtype=np.uint8, copy=True)
        if array.ndim != 2 or array.size == 0:
            raise ValueError('occupancy cells must be a non-empty 2D array')
        if array.max() > OCCUPIED:
            raise ValueError('occupancy cells must be 0 unknown, 1 free or 2 occupied')
        ox, oz = (float(v) for v in origin)
        cell_m = float(cell_m)
        if not (math.isfinite(ox) and math.isfinite(oz) and math.isfinite(cell_m) and cell_m > 0):
            raise ValueError('occupancy origin and cell_m must be finite, cell_m positive')
        array.setflags(write=False)
        return cls(origin=(ox, oz), cell_m=cell_m, cells=array)

    @classmethod
    def from_message(cls, message: dict) -> Grid:
        """Decode a contract ``occupancy`` message (base64 uint8, row-major)."""
        width, height = int(message['width']), int(message['height'])
        raw = base64.b64decode(message['cells'], validate=True)
        if width <= 0 or height <= 0 or len(raw) != width * height:
            raise ValueError('occupancy cells length must equal width * height')
        cells = np.frombuffer(raw, dtype=np.uint8).reshape(height, width)
        return cls.from_array(cells, origin=message['origin'], cell_m=message['cell_m'])

    def cell_of(self, x: float, z: float) -> tuple[int, int]:
        """(row, col) of world (x, z), possibly outside the grid."""
        return (math.floor((z - self.origin[1]) / self.cell_m), math.floor((x - self.origin[0]) / self.cell_m))

    def world_to_cell(self, x: float, z: float) -> tuple[int, int] | None:
        """(row, col) holding world (x, z), or None outside the grid."""
        row, col = self.cell_of(x, z)
        if 0 <= row < self.height and 0 <= col < self.width:
            return row, col
        return None

    def cell_center(self, row: int, col: int) -> tuple[float, float]:
        return (self.origin[0] + (col + .5) * self.cell_m, self.origin[1] + (row + .5) * self.cell_m)


@dataclass(frozen=True)
class PlannerConfig:
    robot_radius_m: float = .15
    margin_m: float = .03
    unknown_traversable: bool = True  # False: unknown is a wall and the grid is not padded
    footprint_clearance: bool = False  # require clearance for every point of a path cell
    unknown_cost: float = 3.  # cost multiplier for unknown cells relative to free (>= 1)
    snap_radius_m: float = .15  # goal snaps to the nearest traversable cell within this
    start_snap_radius_m: float = .30  # the rover itself may sit in an inflation band
    max_expansions: int = 200_000
    waypoint_spacing_m: float = .25
    frontier_min_distance_m: float = .30
    pad_margin_m: float = 1.  # unknown border added around an out-of-map start or goal
    max_grid_cells: int = 1_000_000  # padding never grows the search grid beyond this


@dataclass(frozen=True)
class PlanResult:
    points: list[list[float]]  # [[x, z], ...] from start to goal; empty on failure
    reason: str | None  # None, out_of_bounds, start_blocked, goal_occupied, goal_unknown,
    #                     no_path or search_limit
    expansions: int = 0

    @property
    def ok(self) -> bool:
        return self.reason is None


def path_message(points, *, session_id: str | None = None, map_epoch: int | None = None) -> dict:
    """Contract ``path`` live message; session fields are included only when given."""
    message = dict(version=1, type='path')
    if session_id is not None:
        message.update(session_id=session_id, map_epoch=map_epoch)
    message['points'] = [[float(x), float(z)] for x, z in points]
    return message


def _disk(grid: Grid, config: PlannerConfig) -> list[tuple[int, int]]:
    """Cell offsets within robot radius + margin of a cell center (the inflation kernel)."""
    reach = max(config.robot_radius_m + config.margin_m, 0.) / grid.cell_m + 1e-9
    if config.footprint_clearance:
        r = int(math.ceil(reach)) + 1
        # Minimum distance between two closed cell squares, not their centers.
        return [(dr, dc) for dr in range(-r, r + 1) for dc in range(-r, r + 1)
                if max(abs(dr) - 1, 0) ** 2 + max(abs(dc) - 1, 0) ** 2 <= reach * reach]
    r = int(math.floor(reach))
    return [(dr, dc) for dr in range(-r, r + 1) for dc in range(-r, r + 1) if dr * dr + dc * dc <= reach * reach]


def traversable_mask(grid: Grid, config: PlannerConfig = PlannerConfig()) -> np.ndarray:
    """Traversable cells under the caller's policy.

    ``footprint_clearance`` inflates unknown, occupied and off-grid squares using
    the minimum distance between their edges, keeping the whole path cell clear.
    The legacy simulation policy otherwise inflates occupied centers only.
    """
    if config.footprint_clearance:
        # Outside is unknown too. Padding handles even grids smaller than the footprint.
        offsets = _disk(grid, config)
        pad = max(max(abs(dr), abs(dc)) for dr, dc in offsets)
        unsafe = grid.cells != FREE
        padded = np.pad(unsafe, pad, constant_values=True)
        blocked = unsafe.copy()
        h, w = unsafe.shape
        for dr, dc in offsets:
            blocked |= padded[pad + dr:pad + dr + h, pad + dc:pad + dc + w]
        return ~blocked
    occupied = grid.cells == OCCUPIED
    blocked = occupied.copy()
    h, w = occupied.shape
    for dr, dc in _disk(grid, config):
        if dr or dc:
            blocked[max(dr, 0):h + min(dr, 0), max(dc, 0):w + min(dc, 0)] |= \
                occupied[max(-dr, 0):h + min(-dr, 0), max(-dc, 0):w + min(-dc, 0)]
    if not config.unknown_traversable:
        blocked |= grid.cells == UNKNOWN
    return ~blocked


def _fit(grid: Grid, points, config: PlannerConfig) -> Grid | None:
    """The grid, padded with unknown so every point has pad_margin_m around it; None if not allowed."""
    cells = [grid.cell_of(x, z) for x, z in points]
    if all(0 <= r < grid.height and 0 <= c < grid.width for r, c in cells):
        return grid
    if not config.unknown_traversable:
        return None
    pad = int(math.ceil(config.pad_margin_m / grid.cell_m))
    r0 = min(0, *(r - pad for r, _ in cells))
    c0 = min(0, *(c - pad for _, c in cells))
    r1 = max(grid.height, *(r + pad + 1 for r, _ in cells))
    c1 = max(grid.width, *(c + pad + 1 for _, c in cells))
    if (r1 - r0) * (c1 - c0) > config.max_grid_cells:
        return None
    padded = np.full((r1 - r0, c1 - c0), UNKNOWN, dtype=np.uint8)
    padded[-r0:-r0 + grid.height, -c0:-c0 + grid.width] = grid.cells
    return Grid.from_array(padded, origin=(grid.origin[0] + c0 * grid.cell_m, grid.origin[1] + r0 * grid.cell_m),
                           cell_m=grid.cell_m)


def _snap(grid: Grid, mask: np.ndarray, cell: tuple[int, int], x: float, z: float,
          radius_m: float) -> tuple[int, int] | None:
    """Nearest traversable cell (by center distance to (x, z)) within radius_m, else None."""
    if mask[cell]:
        return cell
    r = int(math.ceil(radius_m / grid.cell_m))
    if r <= 0:
        return None
    row, col = cell
    r0, r1 = max(row - r, 0), min(row + r + 1, grid.height)
    c0, c1 = max(col - r, 0), min(col + r + 1, grid.width)
    rows, cols = np.nonzero(mask[r0:r1, c0:c1])
    if rows.size == 0:
        return None
    cx = grid.origin[0] + (cols + c0 + .5) * grid.cell_m
    cz = grid.origin[1] + (rows + r0 + .5) * grid.cell_m
    d2 = (cx - x) ** 2 + (cz - z) ** 2
    best = int(np.argmin(d2))
    if d2[best] > radius_m * radius_m:
        return None
    return int(rows[best] + r0), int(cols[best] + c0)


def _segment_cells(grid: Grid, a, b):
    """Every (row, col) the world segment a -> b touches, both side cells at exact corners.

    Grid traversal after Amanatides and Woo (1987); cells may lie outside the grid.
    """
    x0, z0 = (a[0] - grid.origin[0]) / grid.cell_m, (a[1] - grid.origin[1]) / grid.cell_m
    x1, z1 = (b[0] - grid.origin[0]) / grid.cell_m, (b[1] - grid.origin[1]) / grid.cell_m
    c, r = math.floor(x0), math.floor(z0)
    c_end, r_end = math.floor(x1), math.floor(z1)
    dx, dz = x1 - x0, z1 - z0
    sc, sr = (1 if dx > 0 else -1), (1 if dz > 0 else -1)
    inf = math.inf
    t_c = ((c + 1 - x0) / dx if dx > 0 else (x0 - c) / -dx) if dx else inf
    t_r = ((r + 1 - z0) / dz if dz > 0 else (z0 - r) / -dz) if dz else inf
    step_c, step_r = (abs(1 / dx) if dx else inf), (abs(1 / dz) if dz else inf)
    yield r, c
    for _ in range(abs(c_end - c) + abs(r_end - r) + 2):
        if (r, c) == (r_end, c_end) or min(t_c, t_r) > 1.:
            return
        if abs(t_c - t_r) < 1e-9:  # through a corner: touch both side cells (no corner cutting)
            yield r + sr, c
            yield r, c + sc
            r, c, t_r, t_c = r + sr, c + sc, t_r + step_r, t_c + step_c
        elif t_c < t_r:
            c, t_c = c + sc, t_c + step_c
        else:
            r, t_r = r + sr, t_r + step_r
        yield r, c


def _astar(mask: np.ndarray, unknown: np.ndarray, start: tuple[int, int], goal: tuple[int, int],
           config: PlannerConfig):
    """8-connected A* with an octile heuristic, unknown cost and no corner cutting.

    Returns (cells, costs, expansions, hit_limit); cells is None when no path was found.
    The grid is padded with a blocked border and flattened so the inner loop needs no
    bounds checks.
    """
    h, w = mask.shape
    width = w + 2
    padded = np.zeros((h + 2, w + 2))
    unknown_cost = max(config.unknown_cost, 1.)
    padded[1:-1, 1:-1] = np.where(mask, np.where(unknown, unknown_cost, 1.), 0.)
    mult = padded.ravel().tolist()
    # Scale the octile heuristic by the cheapest traversable cell so it stays admissible
    # but does not collapse into a full flood on an all-unknown map.
    lowest = 1. if (mask & ~unknown).any() else unknown_cost
    s = (start[0] + 1) * width + start[1] + 1
    g = (goal[0] + 1) * width + goal[1] + 1
    gr, gc = goal[0] + 1, goal[1] + 1
    cost = [math.inf] * len(mult)
    parent = [-1] * len(mult)
    closed = bytearray(len(mult))
    cost[s] = 0.
    moves = [(-width, 1., 0, 0), (width, 1., 0, 0), (-1, 1., 0, 0), (1, 1., 0, 0)]
    moves += [(dr * width + dc, _SQRT2, dr * width, dc) for dr in (-1, 1) for dc in (-1, 1)]
    octile = (_SQRT2 - 1) * lowest
    heap = [(0., 0., s)]
    expansions = 0
    heappush, heappop = heapq.heappush, heapq.heappop
    while heap:
        _, _, cur = heappop(heap)
        if closed[cur]:
            continue
        if cur == g:
            flat = [cur]
            while parent[cur] >= 0:
                cur = parent[cur]
                flat.append(cur)
            flat.reverse()
            return [(n // width - 1, n % width - 1) for n in flat], [cost[n] for n in flat], expansions, False
        if expansions >= config.max_expansions:
            return None, None, expansions, True
        closed[cur] = 1
        expansions += 1
        base = cost[cur]
        for delta, length, side_r, side_c in moves:
            n = cur + delta
            m = mult[n]
            if not m or closed[n] or (side_r and not (mult[cur + side_r] and mult[cur + side_c])):
                continue
            new = base + length * m
            if new < cost[n]:
                cost[n] = new
                parent[n] = cur
                nr, nc = divmod(n, width)
                ar, ac = abs(nr - gr), abs(nc - gc)
                hh = lowest * ar + octile * ac if ar > ac else lowest * ac + octile * ar
                heappush(heap, (new + hh, hh, n))
    return None, None, expansions, False


def _shortcut(grid: Grid, mask: np.ndarray, unknown: np.ndarray, cells, costs, config: PlannerConfig):
    """Greedy line-of-sight shortcutting between the A* path's turning cells.

    A shortcut must stay on traversable cells and must not cost more than the A* stretch
    it replaces, pricing its length by the share of unknown cells it touches, so a route
    deliberately kept on known-free floor is not pulled back into unknown.
    """
    turns = [0] + [i for i in range(1, len(cells) - 1)
                   if (cells[i][0] - cells[i - 1][0], cells[i][1] - cells[i - 1][1])
                   != (cells[i + 1][0] - cells[i][0], cells[i + 1][1] - cells[i][1])] + [len(cells) - 1]
    turns = sorted(set(turns))
    unknown_cost = max(config.unknown_cost, 1.)

    def clear(i, j):
        (ar, ac), (br, bc) = cells[i], cells[j]
        touched = unknown_touched = 0
        for r, c in _segment_cells(grid, grid.cell_center(ar, ac), grid.cell_center(br, bc)):
            if not (0 <= r < grid.height and 0 <= c < grid.width) or not mask[r, c]:
                return False
            touched += 1
            unknown_touched += bool(unknown[r, c])
        price = 1. + (unknown_cost - 1.) * unknown_touched / touched
        return math.hypot(br - ar, bc - ac) * price <= costs[j] - costs[i] + 1e-9

    kept, k = [turns[0]], 0
    while k < len(turns) - 1:
        m = k + 1
        while m + 1 < len(turns) and clear(turns[k], turns[m + 1]):
            m += 1
        kept.append(turns[m])
        k = m
    return [cells[i] for i in kept]


def _segment_clear(grid: Grid, mask: np.ndarray, a, b) -> bool:
    return all(0 <= r < grid.height and 0 <= c < grid.width and mask[r, c] for r, c in _segment_cells(grid, a, b))


def _densify(points, spacing: float) -> list[list[float]]:
    """Split segments so consecutive waypoints are at most ``spacing`` apart."""
    out = [[float(points[0][0]), float(points[0][1])]]
    for (ax, az), (bx, bz) in zip(points, points[1:]):
        n = max(1, math.ceil(math.hypot(bx - ax, bz - az) / spacing - 1e-9))
        for k in range(1, n + 1):
            out.append([ax + (bx - ax) * k / n, az + (bz - az) * k / n])
    return out


def plan_path(grid: Grid, start_xz, goal_xz, config: PlannerConfig = PlannerConfig()) -> PlanResult:
    """Plan from world start (x, z) to goal (x, z); waypoints are ``path`` message ready."""
    sx, sz = (float(v) for v in start_xz)
    gx, gz = (float(v) for v in goal_xz)
    grid = _fit(grid, [(sx, sz), (gx, gz)], config)
    if grid is None:
        return PlanResult([], 'out_of_bounds')
    start_cell, goal_cell = grid.world_to_cell(sx, sz), grid.world_to_cell(gx, gz)
    mask = traversable_mask(grid, config)
    goal = _snap(grid, mask, goal_cell, gx, gz, config.snap_radius_m)
    if goal is None:
        unknown_goal = grid.cells[goal_cell] == UNKNOWN and not config.unknown_traversable
        return PlanResult([], 'goal_unknown' if unknown_goal else 'goal_occupied')
    start = _snap(grid, mask, start_cell, sx, sz, config.start_snap_radius_m)
    if start is None:
        return PlanResult([], 'start_blocked')
    unknown = grid.cells == UNKNOWN
    cells, costs, expansions, hit_limit = _astar(mask, unknown, start, goal, config)
    if cells is None:
        return PlanResult([], 'search_limit' if hit_limit else 'no_path', expansions)
    corners = [grid.cell_center(*cell) for cell in _shortcut(grid, mask, unknown, cells, costs, config)]
    if len(corners) == 1:
        corners.append(corners[0])
    # Keep the exact clicked goal and rover position when their cells and first/last legs are clear.
    if goal == goal_cell and _segment_clear(grid, mask, corners[-2], (gx, gz)):
        corners[-1] = (gx, gz)
    if start == start_cell and _segment_clear(grid, mask, (sx, sz), corners[1]):
        corners[0] = (sx, sz)
    return PlanResult(_densify(corners, config.waypoint_spacing_m), None, expansions)


def path_blocked(grid: Grid, points, config: PlannerConfig = PlannerConfig(), start_index: int = 0) -> bool:
    """True when any remaining leg of ``points`` (from ``start_index``) now crosses a cell
    within radius + margin of an occupied cell, or unknown/off-map space the config forbids.

    Cheap enough to run on every occupancy update: only the cells under the path and their
    inflation neighborhoods are read, never a full-grid inflation.
    """
    legs = [tuple(p) for p in points[max(start_index, 0):]]
    if not legs:
        return False
    if len(legs) == 1:
        legs.append(legs[0])
    touched = set()
    for a, b in zip(legs, legs[1:]):
        touched.update(_segment_cells(grid, a, b))
    rows = np.fromiter((r for r, _ in touched), dtype=np.int64, count=len(touched))
    cols = np.fromiter((c for _, c in touched), dtype=np.int64, count=len(touched))
    inside = (rows >= 0) & (rows < grid.height) & (cols >= 0) & (cols < grid.width)
    if not config.unknown_traversable and (not inside.all() or (grid.cells[rows, cols] == UNKNOWN).any()):
        return True
    rows, cols = rows[inside], cols[inside]
    if config.footprint_clearance:
        # Same all-points policy as A*; include off-grid neighbors in the footprint.
        for dr, dc in _disk(grid, config):
            rr, cc = rows + dr, cols + dc
            if ((rr < 0) | (rr >= grid.height) | (cc < 0) | (cc >= grid.width)).any():
                return True
            if (grid.cells[rr, cc] != FREE).any():
                return True
        return False
    occupied = grid.cells == OCCUPIED
    for dr, dc in _disk(grid, config):
        rr, cc = rows + dr, cols + dc
        ok = (rr >= 0) & (rr < grid.height) & (cc >= 0) & (cc < grid.width)
        if occupied[rr[ok], cc[ok]].any():
            return True
    return False


def _frontier_cells(grid: Grid) -> np.ndarray:
    unknown = np.pad(grid.cells == UNKNOWN, 1, constant_values=True)
    touches_unknown = unknown[:-2, 1:-1] | unknown[2:, 1:-1] | unknown[1:-1, :-2] | unknown[1:-1, 2:]
    return (grid.cells == FREE) & touches_unknown


def _safe_frontiers(grid: Grid, config: PlannerConfig) -> np.ndarray:
    if not config.footprint_clearance:
        return _frontier_cells(grid)
    # Explore the boundary of footprint-clear known floor, staying inside sensing.
    known = Grid.from_array(np.where(grid.cells == UNKNOWN, UNKNOWN, FREE),
                            origin=grid.origin, cell_m=grid.cell_m)
    known_clear = traversable_mask(known, config)
    inset = Grid.from_array(np.where(known_clear, FREE, UNKNOWN),
                            origin=grid.origin, cell_m=grid.cell_m)
    return _frontier_cells(inset)


def is_frontier(grid: Grid, xz, config: PlannerConfig = PlannerConfig()) -> bool:
    """Whether world (x, z) is still a free cell bordering unknown (or unmapped) space."""
    cell = grid.world_to_cell(float(xz[0]), float(xz[1]))
    return cell is not None and bool((_safe_frontiers(grid, config) & traversable_mask(grid, config))[cell])


def nearest_frontier(grid: Grid, start_xz, config: PlannerConfig = PlannerConfig()):
    """World (x, z) of the nearest reachable frontier, or None.

    A frontier is a free, traversable cell with an unknown 4-neighbor; space outside the
    grid counts as unknown, since the published grid is cropped to the mapped area. Reachability is
    an 8-connected breadth-first search over known-free traversable cells (unknown is
    always blocked here, whatever the config says), bounded by
    ``config.max_expansions``; frontiers closer than ``config.frontier_min_distance_m``
    to the start are skipped so the rover does not chase the unmapped floor under itself.
    """
    sx, sz = (float(v) for v in start_xz)
    start_cell = grid.world_to_cell(sx, sz)
    if start_cell is None:
        return None
    mask = traversable_mask(grid, dataclasses.replace(config, unknown_traversable=False))
    start = _snap(grid, mask, start_cell, sx, sz, config.start_snap_radius_m)
    if start is None:
        return None
    frontier = (mask & _safe_frontiers(grid, config)).ravel().tolist()
    h, w = mask.shape
    free = mask.ravel().tolist()
    seen = bytearray(h * w)
    s = start[0] * w + start[1]
    seen[s] = 1
    queue, head = [s], 0
    min_d2 = config.frontier_min_distance_m ** 2
    moves = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))
    while head < len(queue) and head < config.max_expansions:
        cur = queue[head]
        head += 1
        r, c = divmod(cur, w)
        if frontier[cur]:
            x, z = grid.cell_center(r, c)
            if (x - sx) ** 2 + (z - sz) ** 2 >= min_d2:
                return x, z
        for dr, dc in moves:
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w:
                n = nr * w + nc
                if free[n] and not seen[n] and (not dr or not dc or (free[r * w + nc] and free[nr * w + c])):
                    seen[n] = 1
                    queue.append(n)
    return None


def _wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class FollowerConfig:
    cruise_mps: float = .15
    min_mps: float = .04  # keeps creeping toward the goal inside the slow-down radius
    max_yaw_rate_rps: float = .5
    lookahead_m: float = .35
    arrive_tolerance_m: float = .15
    rotate_in_place_rad: float = .6  # beyond this heading error, turn on the spot
    rotate_gain: float = 2.
    slow_radius_m: float = .40
    # Optional look-around before following: the phone camera only faces forward, so one
    # turn on the spot at the yaw-rate limit maps the surroundings first. Progress is
    # measured from the poses passed to step(), not a clock, so a car that is not actually
    # turning never finishes the scan; the caller's stale-progress watchdog must stop it.
    scan_turn: bool = False
    scan_turn_rad: float = 2 * math.pi


@dataclass(frozen=True)
class Command:
    v_mps: float
    yaw_rate_rps: float
    status: str  # follow, rotate, scan, arrived or empty
    target: tuple[float, float] | None = None

    @property
    def arrived(self) -> bool:
        return self.status == 'arrived'


@dataclass
class PurePursuit:
    """Pure-pursuit follower over [[x, z], ...] waypoints; no clock, no I/O.

    ``step`` maps the current pose (x, z, yaw_rad in the backend convention) to one
    clamped command. State is only the path segment already passed (so the lookahead
    never jumps back along a path that doubles back) and the optional scan-turn progress.
    ``segment`` is the index to pass to ``path_blocked`` as ``start_index``.
    """
    path: list
    config: FollowerConfig = field(default_factory=FollowerConfig)
    segment: int = 0
    _scanned_rad: float = 0.
    _last_yaw: float | None = None

    def __post_init__(self):
        self.path = [(float(x), float(z)) for x, z in self.path]

    def replaced(self, path) -> PurePursuit:
        """A follower for a replanned path that keeps any scan-turn progress."""
        return PurePursuit(path, self.config, _scanned_rad=self._scanned_rad, _last_yaw=self._last_yaw)

    def _limits(self) -> tuple[float, float]:
        cfg = self.config
        return (_clamp(cfg.cruise_mps, 0., MAX_SPEED_MPS),
                _clamp(cfg.max_yaw_rate_rps, 0., MAX_YAW_RATE_RPS))

    def _closest(self, x: float, z: float) -> tuple[int, float]:
        """Closest (segment index, fraction) at or after the segment already reached."""
        best = (math.inf, self.segment, 0.)
        for i in range(self.segment, len(self.path) - 1):
            (ax, az), (bx, bz) = self.path[i], self.path[i + 1]
            dx, dz = bx - ax, bz - az
            length2 = dx * dx + dz * dz
            t = 0. if length2 == 0 else _clamp(((x - ax) * dx + (z - az) * dz) / length2, 0., 1.)
            d2 = (ax + dx * t - x) ** 2 + (az + dz * t - z) ** 2
            if d2 < best[0]:
                best = (d2, i, t)
        return best[1], best[2]

    def _lookahead(self, segment: int, t: float, distance: float) -> tuple[float, float]:
        (ax, az), (bx, bz) = self.path[segment], self.path[segment + 1]
        x, z = ax + (bx - ax) * t, az + (bz - az) * t
        for i in range(segment, len(self.path) - 1):
            bx, bz = self.path[i + 1]
            step = math.hypot(bx - x, bz - z)
            if step >= distance:
                return x + (bx - x) * distance / step, z + (bz - z) * distance / step
            distance -= step
            x, z = bx, bz
        return self.path[-1]

    def step(self, x: float, z: float, yaw_rad: float) -> Command:
        cfg = self.config
        if not self.path:
            return Command(0., 0., 'empty')
        goal = self.path[-1]
        remaining = math.hypot(goal[0] - x, goal[1] - z)
        if remaining <= cfg.arrive_tolerance_m:
            return Command(0., 0., 'arrived', goal)
        max_v, max_w = self._limits()
        if cfg.scan_turn and self._scanned_rad < cfg.scan_turn_rad:
            if self._last_yaw is not None:
                self._scanned_rad += _wrap(yaw_rad - self._last_yaw)
            self._last_yaw = yaw_rad
            if self._scanned_rad < cfg.scan_turn_rad:
                return Command(0., max_w, 'scan')
        if len(self.path) == 1:
            target = goal
        else:
            self.segment, t = self._closest(x, z)
            target = self._lookahead(self.segment, t, cfg.lookahead_m)
        dx, dz = target[0] - x, target[1] - z
        alpha = _wrap(math.atan2(dx, dz) - yaw_rad)  # + means target is to the left
        if abs(alpha) > cfg.rotate_in_place_rad:
            return Command(0., _clamp(cfg.rotate_gain * alpha, -max_w, max_w), 'rotate', target)
        distance = max(math.hypot(dx, dz), 1e-6)
        curvature = 2. * math.sin(alpha) / distance
        v = max_v * min(1., remaining / cfg.slow_radius_m) if cfg.slow_radius_m > 0 else max_v
        v = _clamp(v, min(cfg.min_mps, max_v), max_v)
        if abs(v * curvature) > max_w:  # keep the arc, slow down instead of cutting it
            v = max_w / abs(curvature)
        w = _clamp(v * curvature, -max_w, max_w)
        return Command(v, w, 'follow', target)
