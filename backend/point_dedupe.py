"""Per-map voxel memory so live `points` chunks carry mostly newly observed surface.

Worker threads call `VoxelMemory.select` on a frame's candidate points; the event
loop calls `commit` only for chunks it actually published and `reset` when the
map identity changes. `commit` is O(1); folding committed keys into the sorted
arrays (O(n) copies) and trimming happen in the next `select`, off the loop.
"""
from dataclasses import dataclass, replace
import math
import os
import threading

import numpy as np

from .mapping import MappingError, PointChunk

MAX_POINTS_PER_CHUNK = 2500  # dashboards reject larger points chunks
MAX_SAMPLES = 256 * 192  # one full ARKit scene-depth map
MAX_VOXELS_LIMIT = 20_000_000  # 16 bytes per remembered voxel, so at most ~320 MB
_AXIS_BITS = 21  # per-axis voxel index bits in a packed key: +-2^20 voxels (+-20 km at 2 cm)
_OFFSET = 1 << (_AXIS_BITS - 1)


class NoNewPoints(MappingError):
    """Every candidate lies in a voxel already sent recently; publish nothing."""


@dataclass(frozen=True)
class PointSettings:
    """Live point density knobs. voxel_m=0 disables dedupe; refresh_s=0 never resends."""
    voxel_m: float = .02
    samples: int = 10_000
    per_chunk: int = MAX_POINTS_PER_CHUNK
    refresh_s: float = 60.
    max_voxels: int = 2_000_000

    ENV = dict(voxel_m='GODSEYE_POINT_VOXEL_M', samples='GODSEYE_POINT_SAMPLES',
               per_chunk='GODSEYE_POINTS_PER_CHUNK', refresh_s='GODSEYE_POINT_REFRESH_S',
               max_voxels='GODSEYE_POINT_MAX_VOXELS')

    def __post_init__(self):
        checks = dict(voxel_m=(self.voxel_m, 0., 1.), samples=(self.samples, 1, MAX_SAMPLES),
                      per_chunk=(self.per_chunk, 1, MAX_POINTS_PER_CHUNK),
                      refresh_s=(self.refresh_s, 0., math.inf),
                      max_voxels=(self.max_voxels, 1, MAX_VOXELS_LIMIT))
        for field, (value, low, high) in checks.items():
            integer = isinstance(low, int)
            number = type(value) is int if integer else \
                isinstance(value, (int, float)) and not isinstance(value, bool)
            if not number or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{self.ENV[field]} ({field}) must be '
                                 f'{"an integer" if integer else "a finite number"} in [{low}, {high}]')

    @property
    def dedupe(self) -> bool:
        return self.voxel_m > 0

    @classmethod
    def from_env(cls, environ=None) -> 'PointSettings':
        """Read the GODSEYE_POINT* variables; unset or empty keeps the default."""
        environ = os.environ if environ is None else environ
        values = {}
        for field, name in cls.ENV.items():
            text = environ.get(name, '').strip()
            if not text:
                continue
            parse = int if isinstance(getattr(cls, field), int) else float
            try:
                values[field] = parse(text)
            except ValueError:
                raise ValueError(f'{name} ({field}) must be {parse.__name__}, got {text!r}') from None
        return cls(**values)


def voxel_keys(positions: np.ndarray, voxel_m: float) -> np.ndarray:
    """Pack each point's voxel index into one non-negative int64 (21 bits per axis)."""
    cells = np.clip(np.floor(positions / voxel_m), -_OFFSET, _OFFSET - 1).astype(np.int64) + _OFFSET
    return (cells[:, 0] << (2 * _AXIS_BITS)) | (cells[:, 1] << _AXIS_BITS) | cells[:, 2]


def _spread(count: int, limit: int) -> np.ndarray:
    """Indices of at most `limit` of `count` items, evenly spaced (not the first N)."""
    if count <= limit:
        return np.arange(count)
    return np.linspace(0, count - 1, limit).astype(np.int64)


def _subset(chunk: PointChunk, keep: np.ndarray, keys) -> PointChunk:
    positions, colors = chunk.positions[keep], chunk.colors[keep]
    for array in (positions, colors):
        array.setflags(write=False)
    return replace(chunk, positions=positions, colors=colors, voxel_keys=keys)


class VoxelMemory:
    """Voxels already published for the current map: bounded, oldest evicted first.

    Stored as a sorted int64 key array plus a float64 last-sent `t_capture`
    array: 16 bytes per voxel (the default 2,000,000 cap is ~32 MB, briefly
    doubled while a fold copies it). Past the cap the oldest-sent voxels are
    dropped down to 90% of the cap, so trimming is amortized.
    """

    def __init__(self, settings: PointSettings):
        self.settings = settings
        self._lock = threading.Lock()  # guards the fields below; held only briefly
        self._fold_lock = threading.Lock()  # one fold at a time, taken only off the loop
        self._generation = 0
        self._keys = np.empty(0, np.int64)
        self._stamps = np.empty(0, np.float64)
        self._pending = []

    def reset(self):
        """Forget every sent voxel (new session/epoch, or a new viewer)."""
        with self._lock:
            self._generation += 1
            self._keys = np.empty(0, np.int64)
            self._stamps = np.empty(0, np.float64)
            self._pending = []

    def commit(self, keys, t_capture: float):
        """Record voxels of a chunk that was published; cheap enough for the event loop."""
        if keys is not None and len(keys):
            with self._lock:
                self._pending.append((keys, float(t_capture)))

    def select(self, chunk: PointChunk, *, limit: int | None = None) -> PointChunk:
        """Keep one candidate per voxel not sent within refresh_s, evenly capped.

        Raises NoNewPoints when nothing qualifies. The result carries the
        voxel keys to `commit` once the chunk is actually published.
        """
        settings = self.settings
        limit = settings.per_chunk if limit is None else limit
        if not settings.dedupe:
            return _subset(chunk, _spread(len(chunk.positions), limit), None)
        keys = voxel_keys(chunk.positions, settings.voxel_m)
        unique, first = np.unique(keys, return_index=True)
        known, stamps = self._fold()
        recent = np.zeros(unique.size, bool)
        if known.size:
            at = np.minimum(np.searchsorted(known, unique), known.size - 1)
            recent = known[at] == unique
            if settings.refresh_s:
                # A capture clock that went backwards (another phone boot) also counts as old.
                age = chunk.t_capture - stamps[at]
                recent &= (age >= 0) & (age < settings.refresh_s)
        new = np.sort(first[~recent])  # back to image order, so the cap spreads evenly
        if not new.size:
            raise NoNewPoints('every voxel in this frame was sent recently')
        keep = new[_spread(new.size, limit)]
        return _subset(chunk, keep, keys[keep])

    def remembered(self) -> np.ndarray:
        """Sorted keys currently remembered (folds pending commits first)."""
        return self._fold()[0]

    def __len__(self):
        return self.remembered().size

    def _fold(self):
        with self._fold_lock:
            with self._lock:
                generation, keys, stamps, pending = self._generation, self._keys, self._stamps, self._pending
                self._pending = []
            if not pending:
                return keys, stamps
            keys, stamps = self._trim(*_merge(keys, stamps, pending))
            with self._lock:
                if self._generation != generation:  # reset while folding: the old map is gone
                    return self._keys, self._stamps
                self._keys, self._stamps = keys, stamps
            return keys, stamps

    def _trim(self, keys, stamps):
        cap = self.settings.max_voxels
        if keys.size <= cap:
            return keys, stamps
        drop = keys.size - (cap - cap // 10)
        keep = np.ones(keys.size, bool)
        keep[np.argpartition(stamps, drop - 1)[:drop]] = False
        return keys[keep], stamps[keep]


def _merge(keys, stamps, pending):
    """Fold (keys, t) batches into sorted arrays; the latest commit of a key wins."""
    added = np.concatenate([np.asarray(batch, np.int64) for batch, _ in pending])
    times = np.concatenate([np.full(len(batch), t) for batch, t in pending])
    order = np.argsort(added, kind='stable')
    added, times = added[order], times[order]
    last = np.append(added[1:] != added[:-1], True)
    added, times = added[last], times[last]
    at = np.searchsorted(keys, added)
    exists = np.zeros(added.size, bool)
    if keys.size:
        exists = keys[np.minimum(at, keys.size - 1)] == added
    if exists.any():
        stamps = stamps.copy()  # a concurrent select may still hold the old array
        stamps[at[exists]] = times[exists]
    fresh = ~exists
    return np.insert(keys, at[fresh], added[fresh]), np.insert(stamps, at[fresh], times[fresh])
