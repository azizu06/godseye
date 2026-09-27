"""Pure world-point -> 2D occupancy grid for the live map; no I/O, sockets, or hardware.

Evidence lives in a sparse voxel store: 5 cm x 5 cm columns on the floor (x, z) split
into 2 cm height slices of ARKit world Y. Each frame adds at most one hit per voxel,
so a hit count is the number of frames that saw a surface there, not point density.

Floor assumption: the phone is handheld or mounted at an unknown height and the AR
origin is wherever the session started, so the floor height is estimated from the
data. ARKit world Y is gravity-aligned, so the floor is a horizontal plane: a height
slice covering many distinct cells that clearly outnumbers the slices 6-16 cm above
and below it (a wall covers every slice about equally). The lowest such slice is the
floor; a table top or ceiling can only win when no floor has been seen. The estimate
is recomputed from all evidence on every snapshot, so it settles as more floor
appears; until a floor is found nothing is published.

Per cell, relative to that floor: hits within FLOOR_TOL_M are free evidence; hits
from OBSTACLE_MIN_M (or a measured rover threshold less HEIGHT_MARGIN_M, see
backend/calibration.py) to OBSTACLE_MAX_M are obstacle evidence. The gap between the
two bands (rugs, depth noise), anything below the floor, and anything above
OBSTACLE_MAX_M (ceiling, overhangs) are ignored. Occupied wins over free once it has OCCUPIED_MIN_HITS hits
and at least OCCUPIED_FREE_RATIO of the cell's free hits, so single outliers never
flip a cell. Evidence never decays: a removed object stays occupied.

Bounds: cells cover x, z in [-HALF_EXTENT_M, HALF_EXTENT_M) around the AR origin
(at most 400 x 400 cells) and Y in [-4, 4) m; other points are dropped. The voxel
store holds at most MAX_VOXELS entries; new voxels beyond that are dropped.

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
HALF_EXTENT_M = 10.
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


def frame_evidence(positions) -> Evidence:
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
    return Evidence(keys, int(len(p) - inside.sum()))


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
        Unknown, occupied and off-grid cells all block."""
        if not self.ready or self.cell(x, z) != FREE:
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
        window[r0 - rows[0]:r1 - rows[0], c0 - cols[0]:c1 - cols[0]] = self.cells[r0:r1, c0:c1]
        return bool(np.all(window[near] == FREE))


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
        self.accepted_at = None
        self.last_message = None
        self.revision = 0  # bumps whenever evidence changes; navigation replans on it
        self._keys = np.empty(0, np.int64)  # sorted (ix * SIDE + iz) * LEVELS + iy
        self._hits = np.empty(0, np.int32)
        self._dirty = False
        self._last_at = None
        self._last_picture = None
        self._floor_y = None  # Accepted floor in this AR map; reset with the grid.
        self._lock = threading.Lock()

    @property
    def voxels(self) -> int:
        return len(self._keys)

    def add(self, positions, now: float = 0.) -> None:
        """Fold one frame's world points ((N, 3) ARKit meters) into the evidence."""
        self.commit(frame_evidence(positions), now)

    def commit(self, evidence: Evidence, now: float) -> None:
        """Fold one accepted frame in; `now` is a monotonic time in seconds.

        Only array inserts: about 1 ms at MAX_VOXELS, cheap enough for an event loop.
        """
        keys = evidence.keys
        with self._lock:
            self.dropped += evidence.outside
            self.accepted_at = now
            if not len(keys):
                return
            at = np.searchsorted(self._keys, keys)
            seen = at < len(self._keys)
            seen[seen] = self._keys[at[seen]] == keys[seen]
            self._hits[at[seen]] += 1  # keys are unique, so each voxel counts once per frame
            new, at = keys[~seen], at[~seen]
            room = max(0, self.max_voxels - len(self._keys))
            if len(new) > room:
                self.dropped += len(new) - room
                new, at = new[:room], at[:room]
            self._keys = np.insert(self._keys, at, new)
            self._hits = np.insert(self._hits, at, 1)
            self._dirty = True
            self.revision += 1

    def _classify(self, keys, hits):
        # Looking toward walls must not erase a floor already observed in this
        # map. Prefer a fresh valid estimate (including a newly seen lower
        # floor); fall back to its last observed height, never old free cells.
        picture = classify(keys, hits, self.obstacle_from_m)
        if picture is not None:
            with self._lock:
                self._floor_y = picture[3]
            return picture
        with self._lock:
            floor_y = self._floor_y
        return (classify(keys, hits, self.obstacle_from_m, floor_y=floor_y)
                if floor_y is not None else None)

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
        fingerprint = (col0, row0, cells.shape, cells.tobytes())
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
                                 origin, CELL_M, cells, floor_y)


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


def classify(keys: np.ndarray, hits: np.ndarray, obstacle_from_m: float = OBSTACLE_MIN_M, *, floor_y=None):
    """(first column, first row, uint8 cells[rows=z, cols=x], floor_y) of the known area, or None."""
    if not len(keys):
        return None
    levels = keys % _LEVELS
    columns = keys // _LEVELS  # ix * SIDE + iz
    if floor_y is None:
        floor_y = estimate_floor(levels)
    if floor_y is None:
        return None
    height = Y_MIN_M + (levels + .5) / _SLICES_PER_M - floor_y
    free = np.bincount(columns, np.where(np.abs(height) <= FLOOR_TOL_M + _EPS, hits, 0),
                       minlength=_SIDE * _SIDE)
    blocked = np.bincount(columns, np.where((height >= obstacle_from_m - _EPS)
                                            & (height <= OBSTACLE_MAX_M + _EPS), hits, 0),
                          minlength=_SIDE * _SIDE)
    state = np.zeros(_SIDE * _SIDE, np.uint8)
    state[free >= FREE_MIN_HITS] = 1
    state[(blocked >= OCCUPIED_MIN_HITS) & (blocked >= OCCUPIED_FREE_RATIO * free)] = 2
    grid = state.reshape(_SIDE, _SIDE).T  # [iz, ix]: row-major rows along +z, as the dashboard reads
    known = grid != 0
    rows, cols = np.flatnonzero(known.any(axis=1)), np.flatnonzero(known.any(axis=0))
    if not len(rows):
        return None
    cells = np.ascontiguousarray(grid[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1])
    return int(cols[0]), int(rows[0]), cells, floor_y
