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
from OBSTACLE_MIN_M to OBSTACLE_MAX_M are obstacle evidence. The gap between the
two bands (rugs, depth noise), anything below the floor, and anything above
OBSTACLE_MAX_M (ceiling, overhangs) are ignored. Occupied wins over free once it has OCCUPIED_MIN_HITS hits
and at least OCCUPIED_FREE_RATIO of the cell's free hits, so single outliers never
flip a cell. Evidence never decays: a removed object stays occupied.

Bounds: cells cover x, z in [-HALF_EXTENT_M, HALF_EXTENT_M) around the AR origin
(at most 400 x 400 cells) and Y in [-4, 4) m; other points are dropped. The voxel
store holds at most MAX_VOXELS entries; new voxels beyond that are dropped.
"""
import base64
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


class OccupancyGrid:
    """Evidence for one session/epoch. `add` and `message_if_due` are thread-safe."""

    def __init__(self, session, max_voxels: int = MAX_VOXELS):
        self.session = tuple(session)
        self.max_voxels = max_voxels
        self.dropped = 0  # points outside the bounds plus new voxels refused by the cap
        self.last_message = None
        self._keys = np.empty(0, np.int64)  # sorted (ix * SIDE + iz) * LEVELS + iy
        self._hits = np.empty(0, np.int32)
        self._dirty = False
        self._last_at = None
        self._last_picture = None
        self._lock = threading.Lock()

    @property
    def voxels(self) -> int:
        return len(self._keys)

    def add(self, positions) -> None:
        """Fold one frame's world points ((N, 3) ARKit meters) into the evidence."""
        p = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
        with np.errstate(invalid='ignore'):
            ix = np.floor(p[:, 0] * _PER_M) + _SIDE // 2
            iz = np.floor(p[:, 2] * _PER_M) + _SIDE // 2
            iy = np.floor((p[:, 1] - Y_MIN_M) * _SLICES_PER_M)
            inside = ((ix >= 0) & (ix < _SIDE) & (iz >= 0) & (iz < _SIDE)
                      & (iy >= 0) & (iy < _LEVELS))  # NaN and inf compare false
        keys = np.unique((ix[inside].astype(np.int64) * _SIDE + iz[inside].astype(np.int64)) * _LEVELS
                         + iy[inside].astype(np.int64))
        with self._lock:
            self.dropped += int(len(p) - inside.sum())
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
        picture = classify(keys, hits)
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


def classify(keys: np.ndarray, hits: np.ndarray):
    """(first column, first row, uint8 cells[rows=z, cols=x], floor_y) of the known area, or None."""
    if not len(keys):
        return None
    levels = keys % _LEVELS
    columns = keys // _LEVELS  # ix * SIDE + iz
    floor_y = estimate_floor(levels)
    if floor_y is None:
        return None
    height = Y_MIN_M + (levels + .5) / _SLICES_PER_M - floor_y
    free = np.bincount(columns, np.where(np.abs(height) <= FLOOR_TOL_M + _EPS, hits, 0),
                       minlength=_SIDE * _SIDE)
    blocked = np.bincount(columns, np.where((height >= OBSTACLE_MIN_M - _EPS)
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
