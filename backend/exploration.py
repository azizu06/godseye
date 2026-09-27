"""Bounded next-view heuristics and sensor evidence; never grants motion permission.

The predicted gain is the FIRST unknown floor boundary inside the calibrated
camera cone. Occluded/unknown space is not assumed free. Successful observations
use actual 3D depth keys independently of this intentionally limited 2D heuristic.
"""

from collections import Counter, deque
from dataclasses import dataclass, field
import math
import numpy as np
from backend.navigation import Grid, PlannerConfig, traversable_mask
from backend.occupancy import ScanObservation


@dataclass(frozen=True)
class ExploreSettings:
    leg_m: float = 0.75
    view_spacing_m: float = 0.5
    headings: int = 8
    rays: int = 13
    range_m: float = 5.0  # same software depth-validity ceiling as mapping.py
    max_positions: int = 96
    max_expansions: int = 200_000
    max_ray_cells: int = 300_000
    max_views: int = 64
    max_runtime_s: float = 300.0
    settle_s: float = 0.6
    view_timeout_s: float = 8.0
    capture_timeout_s: float = 4.0
    stable_position_m: float = 0.03
    stable_yaw_rad: float = 0.08
    min_frames: int = 3
    min_samples: int = 16
    low_gain_cells: int = 4
    low_gain_surface_voxels: int = 8
    low_gain_views: int = 3
    saturation_views: int = 12
    saturation_low_views: int = 6
    translated_views: int = 3
    max_surface_keys: int = 500_000


def wrap(angle):
    return math.remainder(angle, math.tau)


def usable(observation, settings):
    return (
        observation is not None
        and observation.in_bounds_samples >= settings.min_samples
        and len(observation.surface_keys) >= settings.min_samples
        and all(
            math.isfinite(v)
            for v in (
                *observation.position,
                observation.camera_yaw,
                observation.pitch,
                observation.roll,
                observation.horizontal_fov,
                observation.vertical_fov,
                observation.t_capture,
            )
        )
        and 0 < observation.horizontal_fov < math.pi
        and 0 < observation.vertical_fov < math.pi
    )


def supported_view(observation):
    # A steep/downward or rolled phone needs a different candidate model; fail
    # explicitly instead of treating its horizontal projection as a useful view.
    return (
        abs(observation.pitch) <= math.pi / 6 and abs(observation.roll) <= math.pi / 12
    )


class ViewLedger:
    def __init__(self):
        self.views = (
            {}
        )  # fixed world position/heading -> actual local boundary signature

    @staticmethod
    def key(position, yaw):
        return (
            round(position[0] / 0.1),
            round(position[1] / 0.1),
            round((yaw % math.tau) / (math.pi / 12)) % 24,
        )

    def contains(self, position, yaw, signature):
        return self.views.get(self.key(position, yaw)) == signature

    @property
    def sectors(self):
        return {heading // 6 for _, _, heading in self.views}

    def separated(self, position, spacing):
        return all(
            math.dist(position, (x * 0.1, z * 0.1)) >= spacing for x, z, _ in self.views
        )

    def record(self, position, yaw, signature):
        self.views[self.key(position, yaw)] = signature


class VisibilityLimit(Exception):
    pass


def _floor_in_frame(point, position, yaw, observation, floor_y, limit):
    dx, dz = point[0] - position[0], point[1] - position[1]
    dy = floor_y - observation.position[1]
    sy, cy, sp, cp = (
        math.sin(yaw),
        math.cos(yaw),
        math.sin(observation.pitch),
        math.cos(observation.pitch),
    )
    depth = dx * sy * cp + dy * sp + dz * cy * cp
    right = -dx * cy + dz * sy
    up = -dx * sy * sp + dy * cp - dz * cy * sp
    cr, sr = math.cos(observation.roll), math.sin(observation.roll)
    right, up = right * cr + up * sr, -right * sr + up * cr
    return (
        0.05 <= depth <= limit
        and abs(right) <= depth * math.tan(observation.horizontal_fov / 2)
        and abs(up) <= depth * math.tan(observation.vertical_fov / 2)
    )


def visible_boundary(grid, position, yaw, observation, floor_y, settings, budget=None):
    """World-lattice keys of first unknown cells; supercover corners cannot see through walls."""
    budget = [0] if budget is None else budget
    found = set()
    x, z = position
    for angle in np.linspace(
        yaw - observation.horizontal_fov / 2,
        yaw + observation.horizontal_fov / 2,
        settings.rays,
    ):
        dx, dz = math.sin(angle), math.cos(angle)
        row, col = grid.cell_of(x, z)
        sx, sz = (1 if dx >= 0 else -1), (1 if dz >= 0 else -1)
        tx = (
            ((grid.origin[0] + (col + (sx > 0)) * grid.cell_m - x) / dx)
            if abs(dx) > 1e-12
            else math.inf
        )
        tz = (
            ((grid.origin[1] + (row + (sz > 0)) * grid.cell_m - z) / dz)
            if abs(dz) > 1e-12
            else math.inf
        )
        stepx, stepz = grid.cell_m / max(abs(dx), 1e-12), grid.cell_m / max(
            abs(dz), 1e-12
        )
        group, distance = [(row, col)], 0.0
        while distance <= settings.range_m:
            budget[0] += len(group)
            if budget[0] > settings.max_ray_cells:
                raise VisibilityLimit
            states = [
                (
                    r,
                    c,
                    (
                        int(grid.cells[r, c])
                        if 0 <= r < grid.height and 0 <= c < grid.width
                        else 0
                    ),
                )
                for r, c in group
            ]
            if any(state == 2 for _, _, state in states):
                break
            unknown = [(r, c) for r, c, state in states if state == 0]
            if unknown:
                for r, c in unknown:
                    point = grid.cell_center(r, c)
                    if _floor_in_frame(
                        point, position, yaw, observation, floor_y, settings.range_m
                    ):
                        found.add(
                            (
                                round(point[0] / grid.cell_m - 0.5),
                                round(point[1] / grid.cell_m - 0.5),
                            )
                        )
                break
            if abs(tx - tz) < 1e-9:
                group = [(row, col + sx), (row + sz, col), (row + sz, col + sx)]
                row, col = row + sz, col + sx
                distance = tx
                tx, tz = tx + stepx, tz + stepz
            elif tx < tz:
                col += sx
                group = [(row, col)]
                distance = tx
                tx += stepx
            else:
                row += sz
                group = [(row, col)]
                distance = tz
                tz += stepz
    return frozenset(found)


@dataclass(frozen=True)
class View:
    position: tuple
    yaw: float
    signature: frozenset
    points: list
    path_length: float
    destination: (
        tuple  # intended full view; gain above describes actual short-leg endpoint
    )


@dataclass(frozen=True)
class Selection:
    view: View | None = None
    reason: str | None = None
    search_limited: bool = False


def select_view(
    grid, config, start, camera_yaw, observation, floor_y, ledger, settings
):
    if not supported_view(observation):
        return Selection(reason="scan_view_unsupported")
    mask = traversable_mask(grid, config)
    source = grid.world_to_cell(*start)
    if source is None or not mask[source]:
        return Selection(reason="start_blocked")
    queue, parents = deque([source]), {source: None}
    positions = [source]
    occupied_bins = {
        (
            math.floor(start[0] / settings.view_spacing_m),
            math.floor(start[1] / settings.view_spacing_m),
        )
    }
    truncated = False
    while queue:
        if len(parents) > settings.max_expansions:
            return Selection(reason="scan_search_limit")
        r, c = queue.popleft()
        point = grid.cell_center(r, c)
        bucket = (
            math.floor(point[0] / settings.view_spacing_m),
            math.floor(point[1] / settings.view_spacing_m),
        )
        if bucket not in occupied_bins:
            occupied_bins.add(bucket)
            if len(positions) < settings.max_positions:
                positions.append((r, c))
            else:
                truncated = True
        for dr, dc in (
            (-1, 0),
            (1, 0),
            (0, -1),
            (0, 1),
            (-1, -1),
            (-1, 1),
            (1, -1),
            (1, 1),
        ):
            nxt = (r + dr, c + dc)
            if (
                0 <= nxt[0] < grid.height
                and 0 <= nxt[1] < grid.width
                and mask[nxt]
                and nxt not in parents
                and (not dr or not dc or (mask[r, nxt[1]] and mask[nxt[0], c]))
            ):
                parents[nxt] = (r, c)
                queue.append(nxt)
    best = None
    budget = [0]
    try:
        for cell in positions:
            path = []
            cur = cell
            while cur is not None:
                path.append(grid.cell_center(*cur))
                cur = parents[cur]
            path.reverse()
            path[0] = tuple(start)
            destination = path[-1]
            length = sum(math.dist(a, b) for a, b in zip(path, path[1:]))
            for yaw in np.linspace(0, math.tau, settings.headings, endpoint=False):
                signature = visible_boundary(
                    grid,
                    destination,
                    float(yaw),
                    observation,
                    floor_y,
                    settings,
                    budget,
                )
                if ledger.contains(destination, float(yaw), signature):
                    continue
                # All previously unobserved headings retain value even in a closed
                # floor map: upper surfaces may still be missing from the 3D scan.
                sector = int((yaw % math.tau) / (math.pi / 2))
                spread = 0.08 if sector not in ledger.sectors else 0.0
                if len(ledger.sectors) == 4 and ledger.separated(
                    destination, settings.view_spacing_m
                ):
                    spread += 0.2
                score = (
                    0.1
                    + spread
                    + len(signature) * grid.cell_m
                    - 0.04 * length
                    - 0.01 * abs(wrap(yaw - camera_yaw))
                )
                if best is None or score > best[0]:
                    best = (score, path, float(yaw), signature, length)
    except VisibilityLimit:
        # Work bounds limit a decision, not the usefulness of already evaluated
        # candidates. Retain the best safe candidate; never infer exhaustion.
        truncated = True
    if best is None:
        return Selection(
            reason="scan_candidate_limit" if truncated else "scan_views_exhausted"
        )
    _, path, yaw, signature, length = best
    destination = path[-1]
    clipped = [path[0]]
    used = 0.0
    for a, b in zip(path, path[1:]):
        step = math.dist(a, b)
        if used + step > settings.leg_m:
            ratio = (settings.leg_m - used) / step
            clipped.append((a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio))
            used = settings.leg_m
            break
        clipped.append(b)
        used += step
    # Reserve a separate bounded evaluation for the actual endpoint. A distant
    # view never contributes its unseen predicted gain to this short leg.
    actual = clipped[-1]
    try:
        signature = visible_boundary(grid, actual, yaw, observation, floor_y, settings)
    except VisibilityLimit:
        return Selection(reason="scan_visibility_limit")
    return Selection(
        View(actual, yaw, signature, [list(p) for p in clipped], used, destination),
        search_limited=truncated,
    )


class ScanEvidence:
    def __init__(self, watermark, position, yaw, known_keys, settings):
        self.watermark = watermark
        self.position = position
        self.yaw = yaw
        self.known = known_keys
        self.settings = settings
        self.frames = 0
        self.counts = Counter()
        self.last_capture = watermark
        self.camera_anchor = None

    def accept(self, observation):
        s = self.settings
        if (
            not usable(observation, s)
            or not supported_view(observation)
            or observation.t_capture <= self.last_capture
            or math.dist(
                (observation.position[0], observation.position[2]), self.position
            )
            > s.stable_position_m
            or abs(wrap(observation.camera_yaw - self.yaw)) > s.stable_yaw_rad
        ):
            return False
        if self.camera_anchor is not None:
            first = self.camera_anchor
            if (
                math.dist(observation.position, first.position) > s.stable_position_m
                or abs(observation.pitch - first.pitch) > s.stable_yaw_rad
                or abs(wrap(observation.roll - first.roll)) > s.stable_yaw_rad
            ):
                # An unstable full camera view breaks consecutive corroboration.
                # Keep the fixed anchor: a different view needs a new dwell.
                self.last_capture = observation.t_capture
                self.frames = 0
                self.counts.clear()
                return False
        else:
            self.camera_anchor = observation
        self.last_capture = observation.t_capture
        self.frames += 1
        self.counts.update(
            key for key in set(observation.surface_keys) if not self._near_known(key)
        )
        return True

    def matches_view(self, observation):
        first = self.camera_anchor
        return (
            first is not None
            and usable(observation, self.settings)
            and supported_view(observation)
            and math.dist(observation.position, first.position)
            <= self.settings.stable_position_m
            and abs(wrap(observation.camera_yaw - self.yaw))
            <= self.settings.stable_yaw_rad
            and abs(observation.pitch - first.pitch) <= self.settings.stable_yaw_rad
            and abs(wrap(observation.roll - first.roll)) <= self.settings.stable_yaw_rad
        )

    def _near_known(self, key):
        """Tolerate one mapping voxel of jitter for stopping metrics only.

        Accepted/rendered geometry and motion clearance are never modified.
        Coordinate bounds prevent neighboring keys wrapping across rows.
        """
        if key in self.known:
            return True
        horizontal, y = divmod(key, 400)
        x, z = divmod(horizontal, 400)
        return any(
            (nx * 400 + nz) * 400 + ny in self.known
            for nx in range(max(0, x - 1), min(400, x + 2))
            for nz in range(max(0, z - 1), min(400, z + 2))
            for ny in range(max(0, y - 1), min(400, y + 2))
        )

    @property
    def new_surface_keys(self):
        # Two distinct high-confidence captures corroborate novelty; a singleton
        # depth outlier cannot continually reset the diminishing-gain counter.
        return {key for key, count in self.counts.items() if count >= 2}

    @property
    def ready(self):
        return self.frames >= self.settings.min_frames


def observation_from_frame(frame, evidence):
    """Metadata of the exact frame whose accepted points formed this evidence."""
    if evidence is None:
        return None
    transform = frame.transform
    fx, fy, cx, cy = (frame.image.intrinsics[i] for i in (0, 4, 6, 7))
    if (
        fx <= 0
        or fy <= 0
        or not 0 < cx < frame.image.width
        or not 0 < cy < frame.image.height
    ):
        return None
    horizontal = 2 * min(math.atan(cx / fx), math.atan((frame.image.width - cx) / fx))
    vertical = 2 * min(math.atan(cy / fy), math.atan((frame.image.height - cy) / fy))
    yaw = math.atan2(-transform[8], -transform[10])
    pitch = math.asin(max(-1.0, min(1.0, -transform[9])))
    right = (-math.cos(yaw), 0.0, math.sin(yaw))
    up = (
        -math.sin(yaw) * math.sin(pitch),
        math.cos(pitch),
        -math.cos(yaw) * math.sin(pitch),
    )
    roll = math.atan2(
        sum(transform[i] * up[i] for i in range(3)),
        sum(transform[i] * right[i] for i in range(3)),
    )
    return ScanObservation(
        frame.t_capture,
        frame.frame_id,
        tuple(transform[12:15]),
        yaw,
        horizontal,
        pitch,
        roll,
        tuple(evidence.keys.tolist()),
        len(evidence.keys),
        vertical,
    )


class ScanWitness:
    """A bounded heuristic stopping witness, explicitly weaker than exhaustion."""

    def __init__(self, settings):
        self.settings = settings
        self.views = 0
        self.low = 0
        self.sectors = set()
        self.positions = []

    def record(self, position, yaw, ground_gain, surface_gain):
        s = self.settings
        self.views += 1
        self.low = (
            self.low + 1
            if ground_gain < s.low_gain_cells
            and surface_gain < s.low_gain_surface_voxels
            else 0
        )
        self.sectors.add(int((yaw % math.tau) / (math.pi / 2)))
        if all(math.dist(position, old) >= s.view_spacing_m for old in self.positions):
            self.positions.append(position)

    @property
    def saturated(self):
        s = self.settings
        return (
            self.views >= s.saturation_views
            and self.low >= s.saturation_low_views
            and len(self.sectors) == 4
            and len(self.positions) >= s.translated_views
        )


def untried_frontier(grid, attempted):
    """Conservative whole-map interface check; inaccessible unknown remains partial.

    Unlike a bounded candidate search, this inexpensive full-grid check cannot
    hide an untested free/unknown boundary beyond the candidate/ray budget.
    """
    cells = np.pad(grid.cells, 1, constant_values=0)
    free = cells == 1
    neighbors = np.zeros_like(free)
    neighbors[1:] |= free[:-1]
    neighbors[:-1] |= free[1:]
    neighbors[:, 1:] |= free[:, :-1]
    neighbors[:, :-1] |= free[:, 1:]
    rows, cols = np.nonzero((cells == 0) & neighbors)
    cx, cz = (
        round(grid.origin[0] / grid.cell_m) - 1,
        round(grid.origin[1] / grid.cell_m) - 1,
    )
    return any((int(c + cx), int(r + cz)) not in attempted for r, c in zip(rows, cols))
