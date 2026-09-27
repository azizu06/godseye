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
    preferred_clearance_m: float = .3  # soft space beyond the hard footprint
    clearance_weight: float = 0.  # opt-in route cost; never makes a narrow passage impassable


@dataclass(frozen=True)
class PlanResult:
    points: list[list[float]]  # [[x, z], ...] from start to goal; empty on failure
    reason: str | None  # None, out_of_bounds, start_blocked, goal_occupied, goal_unknown,
    #                     no_path or search_limit
    expansions: int = 0
    fast_corridor: bool = False  # explicitly observed two-wall, centered straight Explore path

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
        unsafe = grid.cells == OCCUPIED if config.unknown_traversable else grid.cells != FREE
        padded = np.pad(unsafe, pad, constant_values=not config.unknown_traversable)
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


def _travel_cost(grid: Grid, mask: np.ndarray, config: PlannerConfig) -> np.ndarray:
    costs = np.where(grid.cells == UNKNOWN, max(config.unknown_cost, 1.), 1.)
    if config.clearance_weight <= 0 or config.preferred_clearance_m <= 0:
        return costs
    # Three soft bands outside the footprint. Route cost and shortcutting use
    # the same field, so smoothing cannot pull a detour back against a wall.
    for fraction in (1., 2 / 3, 1 / 3):
        wider = dataclasses.replace(config, unknown_traversable=True,
                                    margin_m=config.margin_m + fraction * config.preferred_clearance_m)
        costs += (~traversable_mask(grid, wider)) * config.clearance_weight / 3
    return costs


def _astar(mask: np.ndarray, unknown: np.ndarray, start: tuple[int, int], goal: tuple[int, int],
           config: PlannerConfig, travel_cost=None):
    """8-connected A* with an octile heuristic, unknown cost and no corner cutting.

    Returns (cells, costs, expansions, hit_limit); cells is None when no path was found.
    The grid is padded with a blocked border and flattened so the inner loop needs no
    bounds checks.
    """
    h, w = mask.shape
    width = w + 2
    padded = np.zeros((h + 2, w + 2))
    unknown_cost = max(config.unknown_cost, 1.)
    padded[1:-1, 1:-1] = np.where(mask, np.where(unknown, unknown_cost, 1.)
                                 if travel_cost is None else travel_cost, 0.)
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


def _shortcut(grid: Grid, mask: np.ndarray, unknown: np.ndarray, cells, costs, config: PlannerConfig,
              travel_cost=None):
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
        touched = total_cost = 0
        for r, c in _segment_cells(grid, grid.cell_center(ar, ac), grid.cell_center(br, bc)):
            if not (0 <= r < grid.height and 0 <= c < grid.width) or not mask[r, c]:
                return False
            touched += 1
            total_cost += (travel_cost[r, c] if travel_cost is not None
                           else unknown_cost if unknown[r, c] else 1.)
        price = total_cost / touched
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
    travel_cost = _travel_cost(grid, mask, config)
    cells, costs, expansions, hit_limit = _astar(mask, unknown, start, goal, config, travel_cost)
    if cells is None:
        return PlanResult([], 'search_limit' if hit_limit else 'no_path', expansions)
    corners = [grid.cell_center(*cell) for cell in _shortcut(grid, mask, unknown, cells, costs, config,
                                                           travel_cost)]
    if len(corners) == 1:
        corners.append(corners[0])
    # Keep the exact clicked goal and rover position when their cells and first/last legs are clear.
    if goal == goal_cell and _segment_clear(grid, mask, corners[-2], (gx, gz)):
        corners[-1] = (gx, gz)
    if start == start_cell and _segment_clear(grid, mask, (sx, sz), corners[1]):
        corners[0] = (sx, sz)
    return PlanResult(_densify(corners, config.waypoint_spacing_m), None, expansions)


APPROACH_RADIUS_M = 1.5  # an approach stand-off never lands farther than this from the clicked thing
APPROACH_ATTEMPTS = 8  # bounded plan attempts before the original refusal stands
APPROACH_FAR_SIDE_M = .5  # cost added to a stand-off directly behind the target, as seen from the rover


def plan_approach(grid: Grid, start_xz, target_xz, config: PlannerConfig = PlannerConfig(),
                  radius_m: float = APPROACH_RADIUS_M) -> PlanResult:
    """Plan to a clicked thing: the target itself when the planner accepts it, else a stand-off.

    When the target cell is occupied, unknown or inside the inflation band, candidates are
    the planner's own traversable cells within ``radius_m`` of the target, ordered by
    distance to the target plus a penalty for lying behind it along the rover-to-target
    line. Each candidate is planned with the unchanged ``plan_path``, so a stand-off is only
    ever a goal the planner itself accepts. The result's last waypoint is the stand-off; if
    no candidate plans, the target's original refusal is returned.
    """
    direct = plan_path(grid, start_xz, target_xz, config)
    if direct.ok or direct.reason not in ('goal_occupied', 'goal_unknown'):
        return direct
    sx, sz = (float(v) for v in start_xz)
    tx, tz = (float(v) for v in target_xz)
    fitted = _fit(grid, [(sx, sz), (tx, tz)], config)
    if fitted is None:
        return direct
    mask = traversable_mask(fitted, config)
    row, col = fitted.world_to_cell(tx, tz)
    r = int(math.ceil(radius_m / fitted.cell_m))
    r0, r1 = max(row - r, 0), min(row + r + 1, fitted.height)
    c0, c1 = max(col - r, 0), min(col + r + 1, fitted.width)
    rows, cols = np.nonzero(mask[r0:r1, c0:c1])
    if rows.size == 0:
        return direct
    cx = fitted.origin[0] + (cols + c0 + .5) * fitted.cell_m
    cz = fitted.origin[1] + (rows + r0 + .5) * fitted.cell_m
    distance = np.hypot(cx - tx, cz - tz)
    keep = distance <= radius_m
    cx, cz, distance = cx[keep], cz[keep], distance[keep]
    away = math.hypot(sx - tx, sz - tz)
    if away > 1e-9:
        # cos of the angle between target->candidate and target->rover: 1 on the rover's side.
        facing = ((cx - tx) * (sx - tx) + (cz - tz) * (sz - tz)) / (np.maximum(distance, 1e-9) * away)
    else:
        facing = np.ones_like(distance)
    cost = distance + APPROACH_FAR_SIDE_M * (1. - facing) / 2.
    order = np.lexsort((np.hypot(cx - sx, cz - sz), cost))
    failed: list[tuple[float, float]] = []
    attempts = 0
    for i in order:
        point = (float(cx[i]), float(cz[i]))
        # Neighbors of an unreachable stand-off are almost always unreachable too.
        if any(math.hypot(point[0] - fx, point[1] - fz) < .3 for fx, fz in failed):
            continue
        if attempts >= APPROACH_ATTEMPTS:
            break
        attempts += 1
        result = plan_path(grid, (sx, sz), point, config)
        if result.ok:
            return result
        failed.append(point)
    return direct


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
    if config.footprint_clearance:
        # Same all-points policy as A*; include off-grid neighbors in the footprint.
        for dr, dc in _disk(grid, config):
            rr, cc = rows + dr, cols + dc
            ok = (rr >= 0) & (rr < grid.height) & (cc >= 0) & (cc < grid.width)
            if not config.unknown_traversable and not ok.all():
                return True
            cells = grid.cells[rr[ok], cc[ok]]
            if ((cells == OCCUPIED) if config.unknown_traversable else (cells != FREE)).any():
                return True
        return False
    rows, cols = rows[inside], cols[inside]
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
    if not config.footprint_clearance or config.unknown_traversable:
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


def _fit_frontier_mask(grid, start, config, target_mask):
    fitted = _fit(grid, [start], config)
    if fitted is not None and target_mask is not None and fitted is not grid:
        # Fitting expands unknown around an off-grid prototype pose. Preserve
        # the world-coordinate exclusions; newly padded targets have no history.
        mask = np.ones(fitted.cells.shape, bool)
        row, col = fitted.cell_of(*grid.origin)
        mask[row:row + grid.height, col:col + grid.width] = target_mask
        target_mask = mask
    return fitted, target_mask


def nearest_frontier(grid: Grid, start_xz, config: PlannerConfig = PlannerConfig(), *,
                     allow_unknown=False, excluded=(), target_mask=None):
    """World (x, z) of the nearest reachable frontier, or None.

    A frontier is a free, traversable cell with an unknown 4-neighbor; space outside the
    grid counts as unknown, since the published grid is cropped to the mapped area. Reachability is
    an 8-connected breadth-first search over known-free traversable cells (unknown is
    blocked unless the flat-terrain caller explicitly passes allow_unknown), bounded by
    ``config.max_expansions``; frontiers closer than ``config.frontier_min_distance_m``
    to the start are skipped so the rover does not chase the unmapped floor under itself.
    Reached regions in ``excluded`` are skipped within at least half a meter.
    """
    sx, sz = (float(v) for v in start_xz)
    if allow_unknown:
        grid, target_mask = _fit_frontier_mask(grid, (sx, sz), config, target_mask)
        if grid is None:
            return None
    start_cell = grid.world_to_cell(sx, sz)
    if start_cell is None:
        return None
    mask = traversable_mask(grid, dataclasses.replace(config, unknown_traversable=allow_unknown))
    start = _snap(grid, mask, start_cell, sx, sz, config.start_snap_radius_m)
    if start is None:
        return None
    frontier = (mask & _safe_frontiers(grid, config)
                & (target_mask if target_mask is not None else True)).ravel().tolist()
    h, w = mask.shape
    free = mask.ravel().tolist()
    seen = bytearray(h * w)
    s = start[0] * w + start[1]
    seen[s] = 1
    queue, head = [s], 0
    min_d2 = config.frontier_min_distance_m ** 2
    excluded_d2 = max(config.frontier_min_distance_m, .5) ** 2 + 1e-12
    moves = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))
    while head < len(queue) and head < config.max_expansions:
        cur = queue[head]
        head += 1
        r, c = divmod(cur, w)
        if frontier[cur]:
            x, z = grid.cell_center(r, c)
            if ((x - sx) ** 2 + (z - sz) ** 2 >= min_d2
                    and all((x - ex) ** 2 + (z - ez) ** 2 > excluded_d2 for ex, ez in excluded)):
                return x, z
        for dr, dc in moves:
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w:
                n = nr * w + nc
                if free[n] and not seen[n] and (not dr or not dc or (free[r * w + nc] and free[nr * w + c])):
                    seen[n] = 1
                    queue.append(n)
    return None


def preferred_explore_frontier(grid: Grid, start_xz, yaw: float,
                               config: PlannerConfig, *, allow_unknown=False, excluded=(), target_mask=None):
    """Reachable unexplored boundary with forward progress or open-room information gain.

    In an observed two-wall corridor, keep a forward route until it ends. In an
    open room, prefer a large unmapped region that a nearby frontier can reveal;
    a tiny gap ahead must not starve the rest of the room. Reached regions in
    ``excluded`` are skipped within at least half a meter.
    """
    sx, sz = (float(v) for v in start_xz)
    if allow_unknown:
        grid, target_mask = _fit_frontier_mask(grid, (sx, sz), config, target_mask)
        if grid is None:
            return None
    start_cell = grid.world_to_cell(sx, sz)
    if start_cell is None:
        return None
    mask = traversable_mask(grid, dataclasses.replace(config, unknown_traversable=allow_unknown))
    start = _snap(grid, mask, start_cell, sx, sz, config.start_snap_radius_m)
    if start is None:
        return None
    frontier_cells = (mask & _safe_frontiers(grid, config)
                      & (target_mask if target_mask is not None else True)).ravel()
    frontiers = frontier_cells.tolist()
    h, w = mask.shape
    min_d2 = config.frontier_min_distance_m ** 2
    excluded_d2 = max(config.frontier_min_distance_m, .5) ** 2 + 1e-12
    for candidate in np.flatnonzero(frontier_cells):
        r, c = divmod(int(candidate), w)
        x, z = grid.cell_center(r, c)
        if ((x - sx) ** 2 + (z - sz) ** 2 < min_d2
                or any((x - ex) ** 2 + (z - ez) ** 2 <= excluded_d2 for ex, ez in excluded)):
            frontiers[candidate] = False
    remaining_frontiers = sum(frontiers)
    if not remaining_frontiers:
        return None
    free = mask.ravel().tolist()
    seen = bytearray(h * w)
    s = start[0] * w + start[1]
    seen[s] = 1
    queue, head = [s], 0
    forward = (math.sin(yaw), math.cos(yaw))
    side = (forward[1], -forward[0])
    corridor = corridor_alignment(
        grid, (sx, sz), (sx + 2. * forward[0], sz + 2. * forward[1]), yaw, config) is not None
    unknown = np.pad((grid.cells == UNKNOWN).astype(np.int32), ((1, 0), (1, 0)))
    unknown = unknown.cumsum(axis=0).cumsum(axis=1)
    gain_radius = max(1, math.ceil(1.5 / grid.cell_m))
    steps = [0] * (h * w)
    best = None
    best_score = -math.inf
    radius = max(1, math.ceil(1. / grid.cell_m))
    moves = ((-1, 0), (1, 0), (0, -1), (0, 1),
             (-1, -1), (-1, 1), (1, -1), (1, 1))
    while head < len(queue) and head < config.max_expansions:
        cur = queue[head]
        head += 1
        r, c = divmod(cur, w)
        if frontiers[cur]:
            remaining_frontiers -= 1
            x, z = grid.cell_center(r, c)
            dx, dz = x - sx, z - sz
            r0, r1 = max(0, r - radius), min(h, r + radius + 1)
            c0, c1 = max(0, c - radius), min(w, c + radius + 1)
            obstacle_r, obstacle_c = np.nonzero(grid.cells[r0:r1, c0:c1] == OCCUPIED)
            clearance = (min(1., float(np.hypot(obstacle_r + r0 - r,
                                               obstacle_c + c0 - c).min()) * grid.cell_m)
                         if len(obstacle_r) else 1.)
            progress = dx * forward[0] + dz * forward[1]
            lateral = abs(dx * side[0] + dz * side[1])
            if corridor:
                score = 4. * progress - .5 * lateral + 2. * clearance
            else:
                gr0, gr1 = max(0, r - gain_radius), min(h, r + gain_radius + 1)
                gc0, gc1 = max(0, c - gain_radius), min(w, c + gain_radius + 1)
                unseen = (unknown[gr1, gc1] - unknown[gr0, gc1]
                          - unknown[gr1, gc0] + unknown[gr0, gc0])
                information_m = math.sqrt(max(0, int(unseen))) * grid.cell_m
                travel_m = steps[cur] * grid.cell_m
                score = 5. * information_m - .7 * travel_m + .35 * progress + clearance
            if score > best_score:
                best, best_score = (x, z), score
            # Once every candidate has been evaluated, flooding the rest of
            # unknown floor cannot change the score or its BFS tie order.
            if not remaining_frontiers:
                break
        for dr, dc in moves:
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w:
                n = nr * w + nc
                if free[n] and not seen[n] and (not dr or not dc or (free[r * w + nc] and free[nr * w + c])):
                    seen[n] = 1
                    steps[n] = steps[cur] + 1
                    queue.append(n)
    return best


def corridor_alignment(grid: Grid, start_xz, goal_xz, yaw: float,
                       config: PlannerConfig):
    """Observed two-wall hallway's center half a meter ahead and its width/offset."""
    sx, sz = start_xz
    fx, fz = math.sin(yaw), math.cos(yaw)
    dx, dz = goal_xz[0] - sx, goal_xz[1] - sz
    forward = dx * fx + dz * fz
    if forward < .8 or abs(dx * fz - dz * fx) > forward * .5:
        return None
    ax, az = sx + .55 * fx, sz + .55 * fz
    side = (fz, -fx)

    def wall_distance(sign):
        steps = math.ceil(1.5 / grid.cell_m)
        for n in range(1, steps + 1):
            distance = n * grid.cell_m
            cell = grid.world_to_cell(ax + sign * distance * side[0],
                                      az + sign * distance * side[1])
            if cell is None:
                return None
            if grid.cells[cell] == OCCUPIED:
                return distance
        return None

    left, right = wall_distance(-1), wall_distance(1)
    if left is None or right is None:
        return None
    offset = (right - left) / 2
    waypoint = (ax + offset * side[0], az + offset * side[1])
    cell = grid.world_to_cell(*waypoint)
    if cell is None or not traversable_mask(grid, config)[cell]:
        return None
    return waypoint, left + right, offset


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
    rotate_exit_rad: float = .2  # finish a pivot before moving; avoids threshold chatter
    slow_radius_m: float = .40
    # Stock ELEGOO N=2 has straight/turn directions, not independent wheel speeds.
    # A measured hardware profile opts into pivoting first, then driving straight.
    pivot_only: bool = False
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
    _rotating: bool = False

    def __post_init__(self):
        self.path = [(float(x), float(z)) for x, z in self.path]

    def replaced(self, path) -> PurePursuit:
        """A follower for a replanned path that keeps any scan-turn progress."""
        return PurePursuit(path, self.config, _scanned_rad=self._scanned_rad,
                           _last_yaw=self._last_yaw, _rotating=self._rotating)

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

    def step(self, x: float, z: float, yaw_rad: float, *, lookahead_m: float | None = None) -> Command:
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
            target = self._lookahead(self.segment, t, cfg.lookahead_m if lookahead_m is None else lookahead_m)
        dx, dz = target[0] - x, target[1] - z
        alpha = _wrap(math.atan2(dx, dz) - yaw_rad)  # + means target is to the left
        threshold = min(cfg.rotate_exit_rad, cfg.rotate_in_place_rad) if self._rotating else cfg.rotate_in_place_rad
        self._rotating = abs(alpha) > threshold
        if self._rotating:
            turn = math.copysign(max_w, alpha) if cfg.pivot_only else _clamp(cfg.rotate_gain * alpha, -max_w, max_w)
            return Command(0., turn, 'rotate', target)
        distance = max(math.hypot(dx, dz), 1e-6)
        curvature = 2. * math.sin(alpha) / distance
        v = max_v * min(1., remaining / cfg.slow_radius_m) if cfg.slow_radius_m > 0 else max_v
        v = _clamp(v, min(cfg.min_mps, max_v), max_v)
        if cfg.pivot_only:
            return Command(v, 0., 'follow', target)
        if abs(v * curvature) > max_w:  # keep the arc, slow down instead of cutting it
            v = max_w / abs(curvature)
        w = _clamp(v * curvature, -max_w, max_w)
        return Command(v, w, 'follow', target)
