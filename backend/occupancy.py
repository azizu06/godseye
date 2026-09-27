"""Pure world-point -> 2D occupancy grid for the live map; no I/O, sockets, or hardware.

Evidence lives in a sparse voxel store: 5 cm x 5 cm columns on the floor (x, z) split
into 2 cm height slices of ARKit world Y. Each frame adds at most one hit per voxel,
so a hit count is the number of frames that saw a surface there, not point density.

Floor assumption: the phone is handheld or mounted at an unknown height and the AR
origin is wherever the session started, so the floor height is estimated from the
data. ARKit world Y is gravity-aligned, so the floor is a horizontal plane: a height
slice covering many distinct cells that clearly outnumbers the slices 6-16 cm above
and below it (a wall covers every slice about equally). The lowest such slice is the
floor; a table top can still win when no floor has been seen. A candidate less than
5 cm below the accepted camera is rejected for both profiles. The estimate
is recomputed from all evidence on every snapshot, so it settles as more floor
appears; until a floor is found nothing is published. For the explicit flat-terrain
prototype, a same-frame plane below the phone camera takes precedence over an
accumulated ceiling plane.

Per cell, relative to that floor: hits within FLOOR_TOL_M are free evidence; hits
from OBSTACLE_MIN_M (or a measured rover threshold less HEIGHT_MARGIN_M, see
backend/calibration.py) to OBSTACLE_MAX_M are obstacle evidence. The gap between the
two bands (rugs, depth noise), anything below the floor, and anything above
OBSTACLE_MAX_M (ceiling, overhangs) are ignored. Occupied wins over free once it has OCCUPIED_MIN_HITS hits
and at least OCCUPIED_FREE_RATIO of the cell's free hits, so single outliers never
flip a cell. Old obstacle voxels can retire only when two newer accepted raw depth
frames independently prove that their complete projected footprint is empty.

Sparse evidence spans a wide AR world coordinate range; each classified navigation
and wire picture is a 20 m window that follows the accepted camera position, so the
rover does not hit the original 10 m map edge. Y is in [-4, 4) m. The voxel store
holds at most MAX_VOXELS entries; when full, distant stored voxels yield to fresh
nearby evidence so mapping can continue through more hallways.

A frame reaches the grid in two steps: `frame_evidence` does the per-point work
anywhere (a worker thread) without touching any grid, and `OccupancyGrid.commit`
folds it in once the frame has been accepted. The live app commits on its event
loop, after checking that the frame's phone, map and age are still current.
"""
import base64
from dataclasses import dataclass
import math
import threading

import numpy as np

CELL_M = .05  # frozen by docs/INTERFACES.md
VOXEL_H_M = .02
HALF_EXTENT_M = 100_000.  # int64 key packing; far beyond useful ARKit tracking
VIEW_SIDE = 400  # 20 m rolling window; 160k cells stay inside the v1 viewer limit
Y_MIN_M, Y_MAX_M = -4., 4.
MAX_VOXELS = 500_000  # 6 MB of keys and counts
FLOOR_TOL_M = .04
OBSTACLE_MIN_M = .08
OBSTACLE_MAX_M = 1.5
# A voxel's height is its slice center minus a floor estimate built from slice centers,
# so it can read up to one slice below the true surface. Calibrated thresholds allow for it.
HEIGHT_MARGIN_M = VOXEL_H_M
FREE_MIN_HITS = 2
OCCUPIED_MIN_HITS = 3
OCCUPIED_FREE_RATIO = .1
FLOOR_MIN_CELLS = 25  # distinct cells in a 6 cm slab, about 0.06 m2 of floor
FLOOR_PEAK_RATIO = 2.
MIN_CAMERA_FLOOR_M = .05  # Same lower bound as classified-floor wire evidence.
PUBLISH_INTERVAL_S = 1.  # contract rate: at most 1 Hz

_PER_M = round(1 / CELL_M)  # integer scale keeps the 5 cm lattice exact
_SIDE = round(2 * HALF_EXTENT_M * _PER_M)
_SLICES_PER_M = round(1 / VOXEL_H_M)
_LEVELS = round((Y_MAX_M - Y_MIN_M) * _SLICES_PER_M)
_EPS = 1e-9
UNKNOWN, FREE, OCCUPIED = 0, 1, 2


@dataclass(frozen=True)
class Evidence:
    """One frame's voxel hits, not yet part of any grid."""
    keys: np.ndarray  # sorted unique voxel keys, read-only
    outside: int  # points outside the grid bounds
    camera_y: float | None = None  # same-frame ARKit camera height, when available
    camera_xz: tuple[float, float] | None = None
    floor_y: float | None = None  # Classified floor from the accepted same-frame anchor.
    sensing_confidence: float = 1.  # accepted raw depth confidence, normalized [0, 1]


@dataclass(frozen=True)
class DepthView:
    """Accepted frame's raw depth evidence without retaining its RGB image."""
    session_id: str
    map_epoch: int
    t_capture: float
    transform: np.ndarray
    intrinsics: np.ndarray
    image_size: tuple[int, int]
    depth: np.ndarray
    confidence: np.ndarray


def depth_view(frame) -> DepthView:
    return DepthView(frame.session_id, frame.map_epoch, frame.t_capture, frame.transform,
                     frame.intrinsics, frame.image.size, frame.depth, frame.confidence)


def contradicted_obstacle_keys(keys, hits, floor_y, obstacle_from_m, first, second,
                                *, max_candidates=2048, cursor=0):
    """Old occupied voxel centers seen through in both distinct raw depth frames.

    The center 3x3 depth patch must confidently see beyond each old voxel in
    two frames. Nearby unrelated geometry at the padded silhouette edge must
    not keep a departed person in the navigation map indefinitely.
    The caller applies these keys only if the second frame itself is accepted.
    """
    empty = np.empty(0, np.int64)
    if (floor_y is None or first is None or second is None
            or (first.session_id, first.map_epoch) != (second.session_id, second.map_epoch)
            or first.t_capture >= second.t_capture):
        return empty
    levels = keys % _LEVELS
    height = Y_MIN_M + (levels + .5) / _SLICES_PER_M - floor_y
    candidates = np.flatnonzero((hits >= OCCUPIED_MIN_HITS)
                                & (height >= obstacle_from_m - _EPS)
                                & (height <= OBSTACLE_MAX_M + _EPS))
    if not len(candidates):
        return empty
    count = min(len(candidates), max_candidates)
    # Prioritize evidence in the rover's immediate surroundings; walking-person
    # voxels near the route otherwise wait behind thousands of older wall voxels
    # in key order. Keep a rolling share for distant map cleanup.
    columns = keys[candidates] // _LEVELS
    ix, iz = np.divmod(columns, _SIDE)
    x = (ix - _SIDE // 2 + .5) / _PER_M
    z = (iz - _SIDE // 2 + .5) / _PER_M
    camera_x, camera_z = second.transform[0, 3], second.transform[2, 3]
    distance = (x - camera_x) ** 2 + (z - camera_z) ** 2
    near_count = count if count == len(candidates) else max(1, count * 3 // 4)
    near = np.argpartition(distance, near_count - 1)[:near_count]
    far_count = count - near_count
    rolling = (np.arange(far_count) + cursor) % len(candidates)
    selected = candidates[np.unique(np.concatenate((near, rolling)))]
    old = keys[selected]
    columns = old // _LEVELS
    ix, iz = np.divmod(columns, _SIDE)
    world = np.column_stack(((ix - _SIDE // 2 + .5) / _PER_M,
                             Y_MIN_M + (old % _LEVELS + .5) / _SLICES_PER_M,
                             (iz - _SIDE // 2 + .5) / _PER_M))

    def visible_through(view):
        depth, confidence = view.depth, view.confidence
        dh, dw = depth.shape
        iw, ih = view.image_size
        if (depth.size > 65536 or confidence.shape != depth.shape or iw <= 0 or ih <= 0):
            return np.zeros(len(old), bool)
        camera = (world - view.transform[:3, 3]) @ view.transform[:3, :3]
        optical = -camera[:, 2]
        with np.errstate(divide='ignore', invalid='ignore'):
            u = (view.intrinsics[0, 0] * camera[:, 0] / optical
                 + view.intrinsics[0, 2]) * dw / iw - .5
            v = (-view.intrinsics[1, 1] * camera[:, 1] / optical
                 + view.intrinsics[1, 2]) * dh / ih - .5
            # The bounding sphere covers any stored point inside this voxel.
            radius = math.sqrt((CELL_M / 2) ** 2 * 2 + (VOXEL_H_M / 2) ** 2)
            pixels = max(view.intrinsics[0, 0] * dw / iw,
                         view.intrinsics[1, 1] * dh / ih) * radius / (optical - radius) + 1
        valid = ((optical > radius + .05) & (optical <= 5 - radius)
                 & np.isfinite(u) & np.isfinite(v) & np.isfinite(pixels))
        result = np.zeros(len(old), bool)
        for i in np.flatnonzero(valid):
            left, right = round(u[i]) - 1, round(u[i]) + 1
            top, bottom = round(v[i]) - 1, round(v[i]) + 1
            if left < 0 or top < 0 or right >= dw or bottom >= dh:
                continue
            d = depth[top:bottom + 1, left:right + 1]
            c = confidence[top:bottom + 1, left:right + 1]
            threshold = optical[i] + radius + max(.12, optical[i] * .05)
            result[i] = bool(np.all((c >= 1) & np.isfinite(d) & (d <= 5) & (d > threshold)))
        return result

    return old[visible_through(first) & visible_through(second)]


def frame_evidence(positions, *, camera_y: float | None = None,
                   camera_xz: tuple[float, float] | None = None,
                   floor_y: float | None = None) -> Evidence:
    """Voxelize one frame's world points ((N, 3) ARKit meters); pure, so safe on any thread."""
    p = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    with np.errstate(invalid='ignore'):
        ix = np.floor(p[:, 0] * _PER_M) + _SIDE // 2
        iz = np.floor(p[:, 2] * _PER_M) + _SIDE // 2
        iy = np.floor((p[:, 1] - Y_MIN_M) * _SLICES_PER_M)
        inside = ((ix >= 0) & (ix < _SIDE) & (iz >= 0) & (iz < _SIDE)
                  & (iy >= 0) & (iy < _LEVELS))  # NaN and inf compare false
    keys = np.unique((ix[inside].astype(np.int64) * _SIDE + iz[inside].astype(np.int64)) * _LEVELS
                     + iy[inside].astype(np.int64))
    keys.setflags(write=False)
    return Evidence(keys, int(len(p) - inside.sum()), camera_y, camera_xz, floor_y)


@dataclass(frozen=True)
class OccupancySnapshot:
    """One session's classified evidence with the rover's readiness, for navigation.

    `cells[row, col]` is laid out like the `/live` message: row along +z from
    origin[1], column along +x from origin[0]. `cells` is None before a floor is found.
    `revision` and `accepted_at` are the grid's, read with the same evidence.
    """
    session: tuple
    revision: int
    accepted_at: float | None
    blockers: tuple  # empty only when the map may be used for motion
    inflation_m: float | None  # rover footprint disc plus margin, from the calibration
    origin: tuple | None
    cell_m: float
    cells: np.ndarray | None
    floor_y: float | None
    unknown_traversable: bool = False  # Explicit flat-terrain prototype only.
    sensing_confidence: float = 1.

    @property
    def ready(self) -> bool:
        return not self.blockers

    def _index(self, x, z):
        return math.floor((z - self.origin[1]) / self.cell_m), math.floor((x - self.origin[0]) / self.cell_m)

    def cell(self, x: float, z: float) -> int:
        """State of the cell holding world (x, z); UNKNOWN outside the grid."""
        if self.cells is None or not (math.isfinite(x) and math.isfinite(z)):
            return UNKNOWN
        row, col = self._index(x, z)
        if 0 <= row < self.cells.shape[0] and 0 <= col < self.cells.shape[1]:
            return int(self.cells[row, col])
        return UNKNOWN

    def traversable(self, x: float, z: float) -> bool:
        """Whether the rover may stand at world (x, z): only on a motion-ready map, and
        only when every cell with any part within `inflation_m` of (x, z) is known free.
        Unknown and off-grid cells block except in the explicit flat-terrain
        prototype. Observed obstacles always block the entire footprint."""
        if not self.ready or not (math.isfinite(x) and math.isfinite(z)):
            return False
        if self.cell(x, z) != FREE and not self.unknown_traversable:
            return False
        row, col = self._index(x, z)
        reach = math.ceil(self.inflation_m / self.cell_m)
        rows, cols = np.arange(row - reach, row + reach + 1), np.arange(col - reach, col + reach + 1)
        # Distance from (x, z) to the nearest point of each cell in the window.
        left, top = self.origin[0] + cols * self.cell_m, self.origin[1] + rows * self.cell_m
        dx = np.maximum(np.maximum(left - x, x - left - self.cell_m), 0)
        dz = np.maximum(np.maximum(top - z, z - top - self.cell_m), 0)
        near = dz[:, None] ** 2 + dx[None, :] ** 2 <= self.inflation_m ** 2
        height, width = self.cells.shape
        window = np.full(near.shape, UNKNOWN, np.uint8)  # off-grid cells stay unknown
        r0, r1 = max(rows[0], 0), min(rows[-1] + 1, height)
        c0, c1 = max(cols[0], 0), min(cols[-1] + 1, width)
        if r0 < r1 and c0 < c1:
            window[r0 - rows[0]:r1 - rows[0], c0 - cols[0]:c1 - cols[0]] = self.cells[r0:r1, c0:c1]
        return bool(np.all(window[near] != OCCUPIED) if self.unknown_traversable
                    else np.all(window[near] == FREE))


class OccupancyGrid:
    """Evidence for one session/epoch. `add`, `commit`, `snapshot` and `message_if_due` are thread-safe.

    `revision` bumps whenever a commit adds evidence (navigation rechecks its path on
    it). `accepted_at` is the caller's monotonic time of the newest committed frame and
    advances even when that frame adds nothing, so readers can tell a still-watched
    map from a stale one.

    `calibration` is a backend.calibration.RoverCalibration or None. Only a complete,
    verified, supported one replaces the generic OBSTACLE_MIN_M, with the rover's
    measured threshold less HEIGHT_MARGIN_M, for `/live`, the planner and the map
    snapshot alike.
    """

    def __init__(self, session, max_voxels: int = MAX_VOXELS, calibration=None):
        self.session = tuple(session)
        self.max_voxels = max_voxels
        self.calibration = calibration
        usable = calibration is not None and not calibration.blockers
        self.obstacle_from_m = calibration.obstacle_min_m - HEIGHT_MARGIN_M if usable else OBSTACLE_MIN_M
        self.dropped = 0  # points outside the bounds plus new voxels refused by the cap
        self.evicted = 0  # distant world evidence replaced by nearby new voxels
        self.accepted_at = None
        self.sensing_confidence = 1.
        self.last_message = None
        self.revision = 0  # bumps whenever evidence changes; navigation replans on it
        self._keys = np.empty(0, np.int64)  # sorted (ix * SIDE + iz) * LEVELS + iy
        self._hits = np.empty(0, np.int32)
        self._dirty = False
        self._last_at = None
        self._last_picture = None
        self._floor_y = None  # Accepted floor in this AR map; reset with the grid.
        self._floor_from_anchor = False
        self._camera_y = None
        self._camera_xz = (0., 0.)
        self._last_depth_view = None
        self._retirement_cursor = 0
        self._mesh_keys = np.empty(0, np.int64)  # latest ARKit reconstruction only
        self._mesh_at = None
        self._lock = threading.Lock()

    @property
    def voxels(self) -> int:
        return len(self._keys)

    def add(self, positions, now: float = 0.) -> None:
        """Fold one frame's world points ((N, 3) ARKit meters) into the evidence."""
        self.commit(frame_evidence(positions), now)

    def clear_depth_history(self):
        """Break two-view proof on phone disconnect or loss of AR tracking."""
        with self._lock:
            self._last_depth_view = None
            self._retirement_cursor = 0

    def retirement_candidates(self, current: DepthView):
        """Read-only proof in the map worker; a discarded frame changes no grid state."""
        with self._lock:
            first = self._last_depth_view
            cursor = self._retirement_cursor
            keys, hits, floor_y = self._keys.copy(), self._hits.copy(), self._floor_y
        if (first is None or (current.session_id, current.map_epoch) != self.session
                or current.t_capture <= first.t_capture):
            return np.empty(0, np.int64), cursor
        if floor_y is None and len(keys):
            floor_y = estimate_floor(keys % _LEVELS)
        retired = contradicted_obstacle_keys(keys, hits, floor_y, self.obstacle_from_m,
                                             first, current, cursor=cursor)
        return retired, cursor + 512

    def mesh_keys_consistent_with_depth(self, mesh_keys, current: DepthView):
        """Drop current ARKit mesh voxels seen through in two fresh depth views.

        ARKit can retain a person's mesh anchor after they walk away. Uncertain
        depth or one clear frame cannot erase a real thin obstacle.
        """
        keys = np.asarray(mesh_keys, dtype=np.int64)
        if not len(keys):
            return keys
        with self._lock:
            first = self._last_depth_view
            floor_y = self._floor_y
            cursor = self._retirement_cursor
            stored = self._keys.copy() if floor_y is None else None
        if first is None or (current.session_id, current.map_epoch) != self.session:
            return keys
        if floor_y is None and len(stored):
            floor_y = estimate_floor(stored % _LEVELS)
        contradicted = contradicted_obstacle_keys(
            keys, np.full(len(keys), OCCUPIED_MIN_HITS, np.int32), floor_y,
            self.obstacle_from_m, first, current, max_candidates=1024,
            cursor=cursor)
        return keys[~np.isin(keys, contradicted, assume_unique=True)]

    def commit(self, evidence: Evidence, now: float, *, retirement_keys=None,
               retirement_cursor=None, depth_view: DepthView | None = None,
               mesh_keys=None) -> None:
        """Fold one accepted frame in; `now` is a monotonic time in seconds.

        Only array inserts: about 1 ms at MAX_VOXELS, cheap enough for an event loop.
        """
        keys = evidence.keys
        with self._lock:
            mesh_changed = False
            if mesh_keys is not None:
                current_mesh = np.unique(np.asarray(mesh_keys, dtype=np.int64))
                if len(current_mesh) > 4000:
                    raise ValueError('mesh snapshot exceeds 4000 voxels')
                mesh_changed = not np.array_equal(self._mesh_keys, current_mesh)
                self._mesh_keys = current_mesh
                self._mesh_at = now
            if depth_view is not None:
                if (depth_view.session_id, depth_view.map_epoch) != self.session:
                    raise ValueError('depth view belongs to another map')
                self._last_depth_view = depth_view
                if retirement_cursor is not None:
                    self._retirement_cursor = retirement_cursor
            removed = 0
            if retirement_keys is not None and len(retirement_keys):
                keep = ~np.isin(self._keys, retirement_keys, assume_unique=True)
                removed = len(self._keys) - int(keep.sum())
                if removed:
                    self._keys, self._hits = self._keys[keep], self._hits[keep]
            if evidence.camera_y is not None and math.isfinite(evidence.camera_y):
                self._camera_y = evidence.camera_y
                # A flat-terrain rover's floor is below its camera. Prefer the
                # plane in a fresh frame over a ceiling dominating old voxels.
                if (evidence.floor_y is not None and math.isfinite(evidence.floor_y)
                        and Y_MIN_M <= evidence.floor_y < Y_MAX_M
                        and .05 <= evidence.camera_y - evidence.floor_y <= 1.5):
                    self._floor_y = evidence.floor_y
                    self._floor_from_anchor = True
                if (not self._floor_from_anchor
                        and getattr(self.calibration, 'unknown_traversable', False)):
                    candidate = estimate_floor(keys % _LEVELS) if len(keys) else None
                    if candidate is not None and evidence.camera_y - candidate >= MIN_CAMERA_FLOOR_M:
                        if self._floor_y is None or candidate <= self._floor_y + .15:
                            self._floor_y = candidate
            if (evidence.camera_xz is not None and len(evidence.camera_xz) == 2
                    and all(math.isfinite(value) for value in evidence.camera_xz)):
                self._camera_xz = evidence.camera_xz
            self.dropped += evidence.outside
            self.accepted_at = now
            self.sensing_confidence = evidence.sensing_confidence
            if not len(keys):
                if removed or mesh_changed:
                    self._dirty = True
                    self.revision += 1
                return
            at = np.searchsorted(self._keys, keys)
            seen = at < len(self._keys)
            seen[seen] = self._keys[at[seen]] == keys[seen]
            self._hits[at[seen]] += 1  # keys are unique, so each voxel counts once per frame
            new, at = keys[~seen], at[~seen]
            room = max(0, self.max_voxels - len(self._keys))
            if len(new) > room:
                if len(new) > self.max_voxels:
                    self.dropped += len(new) - self.max_voxels
                    new = new[:self.max_voxels]
                # Free a batch so each following capture does not sort a full
                # memory of distant voxels on the event loop again.
                evict = min(len(self._keys), max(0, len(new) - room,
                                                 max(1, self.max_voxels // 10)))
                if evict:
                    columns = self._keys // _LEVELS
                    ix, iz = np.divmod(columns, _SIDE)
                    cx = math.floor(self._camera_xz[0] * _PER_M) + _SIDE // 2
                    cz = math.floor(self._camera_xz[1] * _PER_M) + _SIDE // 2
                    distance = (ix.astype(np.float64) - cx) ** 2 + (iz.astype(np.float64) - cz) ** 2
                    keep = np.ones(len(self._keys), bool)
                    if evict == len(keep):
                        keep[:] = False
                    else:
                        keep[np.argpartition(distance, len(keep) - evict)[len(keep) - evict:]] = False
                    self._keys, self._hits = self._keys[keep], self._hits[keep]
                    self.evicted += evict
                at = np.searchsorted(self._keys, new)
            self._keys = np.insert(self._keys, at, new)
            self._hits = np.insert(self._hits, at, 1)
            self._dirty = True
            self.revision += 1

    def _classify(self, keys, hits):
        # A ceiling can dominate glossy-floor depth. Neither measured nor
        # prototype navigation may call a plane above the same-frame camera a
        # floor. Require the same 5 cm minimum camera separation as anchor evidence.
        # A classified anchor wins over this depth-only heuristic.
        with self._lock:
            floor_y, camera_y, camera_xz = self._floor_y, self._camera_y, self._camera_xz
            anchored, revision = self._floor_from_anchor, self.revision
            mesh_keys = self._mesh_keys.copy() if (self._mesh_at is not None
                and self.accepted_at is not None and self.accepted_at - self._mesh_at <= 2.5) else None
        if mesh_keys is not None and len(mesh_keys):
            keys = np.concatenate((keys, mesh_keys))
            hits = np.concatenate((hits, np.full(len(mesh_keys), OCCUPIED_MIN_HITS, np.int32)))
        if floor_y is not None and camera_y is not None and camera_y - floor_y < MIN_CAMERA_FLOOR_M:
            floor_y = None
        if not anchored:
            candidate = estimate_floor(keys % _LEVELS) if len(keys) else None
            if candidate is not None and (camera_y is None or camera_y - candidate >= MIN_CAMERA_FLOOR_M):
                # Preserve the prototype's flat-terrain floor against elevated
                # furniture; measured depth may refine a valid existing floor.
                if (floor_y is None or not getattr(self.calibration, 'unknown_traversable', False)
                        or candidate <= floor_y + .15):
                    floor_y = candidate
        if floor_y is None:
            return None
        picture = classify(keys, hits, self.obstacle_from_m, floor_y=floor_y, center_xz=camera_xz)
        if picture is not None and mesh_keys is not None and len(mesh_keys):
            col0, row0, cells, floor = picture
            ix, iz = np.divmod(mesh_keys // _LEVELS, _SIDE)
            height = Y_MIN_M + (mesh_keys % _LEVELS + .5) / _SLICES_PER_M - floor
            obstacle = ((height >= self.obstacle_from_m - _EPS)
                        & (height <= OBSTACLE_MAX_M + _EPS)
                        & (ix >= col0) & (ix < col0 + cells.shape[1])
                        & (iz >= row0) & (iz < row0 + cells.shape[0]))
            cells[iz[obstacle] - row0, ix[obstacle] - col0] = OCCUPIED
        if picture is not None:
            with self._lock:
                # A snapshot worker must not overwrite an anchor committed
                # while it was classifying older depth evidence.
                if self.revision == revision and not self._floor_from_anchor:
                    self._floor_y = floor_y
        return picture

    def snapshot(self):
        """(revision, origin [x, z] or None, uint8 cells[rows=z, cols=x] or None), unthrottled.

        The same classification as the published grid, for the planner. Blocking like
        `message_if_due`; it never touches the publish rate limit or change detection.
        """
        with self._lock:
            revision, keys, hits = self.revision, self._keys.copy(), self._hits.copy()
        picture = self._classify(keys, hits)
        if picture is None:
            return revision, None, None
        col0, row0, cells, _ = picture
        return revision, ((col0 - _SIDE // 2) / _PER_M, (row0 - _SIDE // 2) / _PER_M), cells

    def message_if_due(self, now: float):
        """The `/live` `occupancy` message, or None when rate-limited, unchanged, or empty.

        `now` is a monotonic time in seconds; at most one message per PUBLISH_INTERVAL_S.
        Blocking for up to MAX_VOXELS entries, so async callers use a worker thread.
        """
        with self._lock:
            if self._last_at is not None and now - self._last_at < PUBLISH_INTERVAL_S:
                return None
            if not self._dirty:
                return None
            self._dirty = False
            keys, hits = self._keys.copy(), self._hits.copy()
        picture = self._classify(keys, hits)
        if picture is None:
            return None
        col0, row0, cells, floor_y = picture
        fingerprint = (col0, row0, cells.shape, cells.tobytes(), floor_y)
        with self._lock:
            if fingerprint == self._last_picture:
                return None
            session_id, map_epoch = self.session
            message = dict(version=1, type='occupancy', session_id=session_id, map_epoch=map_epoch,
                           origin=[(col0 - _SIDE // 2) / _PER_M, (row0 - _SIDE // 2) / _PER_M],
                           cell_m=CELL_M, width=int(cells.shape[1]), height=int(cells.shape[0]),
                           cells=base64.b64encode(cells.tobytes()).decode('ascii'),
                           floor_y=round(floor_y, 3))
            self._last_picture, self._last_at, self.last_message = fingerprint, now, message
            return message

    def map_snapshot(self) -> OccupancySnapshot:
        """Classified picture of all accepted evidence, with readiness; blocking like `snapshot`."""
        with self._lock:
            revision, accepted_at = self.revision, self.accepted_at
            sensing_confidence = self.sensing_confidence
            keys, hits = self._keys.copy(), self._hits.copy()
        picture = self._classify(keys, hits)
        origin = cells = floor_y = None
        if picture is not None:
            col0, row0, cells, floor_y = picture
            origin = ((col0 - _SIDE // 2) / _PER_M, (row0 - _SIDE // 2) / _PER_M)
            cells.setflags(write=False)
        calibration = self.calibration
        blockers = ('calibration_missing',) if calibration is None else calibration.blockers
        if picture is None:
            blockers += ('no_floor',)
        return OccupancySnapshot(self.session, revision, accepted_at, blockers,
                                 None if calibration is None else calibration.inflation_m,
                                 origin, CELL_M, cells, floor_y,
                                 getattr(calibration, 'unknown_traversable', False), sensing_confidence)


def estimate_floor(levels: np.ndarray):
    """World Y of the lowest horizontal plane among voxel height slices, or None."""
    area = np.bincount(levels, minlength=_LEVELS).astype(np.float64)
    slab = np.convolve(area, np.ones(3), mode='same')  # 6 cm slabs absorb depth noise
    total = np.concatenate([[0.], np.cumsum(np.concatenate([np.zeros(9), slab, np.zeros(9)]))])
    index = np.arange(_LEVELS) + 9

    def mean(lo, hi):  # mean slab area over slices [i + lo, i + hi)
        return (total[index + hi] - total[index + lo]) / (hi - lo)

    peak = (slab >= FLOOR_MIN_CELLS) & (slab >= FLOOR_PEAK_RATIO * np.maximum(mean(3, 9), mean(-8, -2)))
    candidates = np.flatnonzero(peak)
    if not len(candidates):
        return None
    i = int(candidates[0])
    while i + 1 < _LEVELS and slab[i + 1] > slab[i]:
        i += 1  # climb from the plane's lower edge to its densest slice
    lo, hi = max(i - 1, 0), min(i + 2, _LEVELS)
    centers = Y_MIN_M + (np.arange(lo, hi) + .5) / _SLICES_PER_M
    return float(np.average(centers, weights=area[lo:hi]))


def classify(keys: np.ndarray, hits: np.ndarray, obstacle_from_m: float = OBSTACLE_MIN_M, *,
             floor_y=None, center_xz=(0., 0.)):
    """(first column, first row, uint8 cells[rows=z, cols=x], floor_y) of the known area, or None."""
    if not len(keys):
        return None
    levels = keys % _LEVELS
    columns = keys // _LEVELS  # ix * SIDE + iz
    if floor_y is None:
        floor_y = estimate_floor(levels)
    if floor_y is None:
        return None
    center_col = math.floor(center_xz[0] * _PER_M) + _SIDE // 2
    center_row = math.floor(center_xz[1] * _PER_M) + _SIDE // 2
    col_start = max(0, min(_SIDE - VIEW_SIDE, center_col - VIEW_SIDE // 2))
    row_start = max(0, min(_SIDE - VIEW_SIDE, center_row - VIEW_SIDE // 2))
    ix, iz = np.divmod(columns, _SIDE)
    inside = ((ix >= col_start) & (ix < col_start + VIEW_SIDE)
              & (iz >= row_start) & (iz < row_start + VIEW_SIDE))
    if not np.any(inside):
        return None
    levels, hits = levels[inside], hits[inside]
    columns = (ix[inside] - col_start) * VIEW_SIDE + iz[inside] - row_start
    height = Y_MIN_M + (levels + .5) / _SLICES_PER_M - floor_y
    free = np.bincount(columns, np.where(np.abs(height) <= FLOOR_TOL_M + _EPS, hits, 0),
                       minlength=VIEW_SIDE * VIEW_SIDE)
    blocked = np.bincount(columns, np.where((height >= obstacle_from_m - _EPS)
                                            & (height <= OBSTACLE_MAX_M + _EPS), hits, 0),
                          minlength=VIEW_SIDE * VIEW_SIDE)
    state = np.zeros(VIEW_SIDE * VIEW_SIDE, np.uint8)
    state[free >= FREE_MIN_HITS] = 1
    state[(blocked >= OCCUPIED_MIN_HITS) & (blocked >= OCCUPIED_FREE_RATIO * free)] = 2
    grid = state.reshape(VIEW_SIDE, VIEW_SIDE).T  # [iz, ix]: rows along +z
    known = grid != 0
    rows, cols = np.flatnonzero(known.any(axis=1)), np.flatnonzero(known.any(axis=0))
    if not len(rows):
        return None
    cells = np.ascontiguousarray(grid[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1])
    return int(col_start + cols[0]), int(row_start + rows[0]), cells, floor_y
