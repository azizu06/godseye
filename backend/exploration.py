"""Bounded session-scoped exploration memory in AR world X/Z, never a safety map.

Visited/failed regions affect only frontier choice: returning through visited
floor remains possible. Occupancy and footprint checks alone authorize motion.
"""
from collections import OrderedDict
import math
import threading

import numpy as np


class ExplorationMemory:
    def __init__(self, cell_m=.25, max_cells=50_000):
        self.cell_m, self.max_cells = cell_m, max_cells
        self._lock = threading.Lock()
        self.session = None
        self.visited = OrderedDict()
        self.failed = OrderedDict()
        self._geometry = None
        self._poses = OrderedDict()
        self._last = None
        self._distance = 0.
        self.novel = 0
        self.loops = 0

    def _session(self, session):
        if session != self.session:
            self.session = session
            self.visited.clear()
            self.failed.clear()
            self._poses.clear()
            self._last, self._distance = None, 0.
            self.novel = self.loops = 0
            self._geometry = None

    def _key(self, x, z):
        return math.floor(x / self.cell_m), math.floor(z / self.cell_m)

    def observe(self, session, x, z, yaw):
        """Record accepted pose travel; True means a closed lap added no new cells."""
        with self._lock:
            self._session(session)
            key = self._key(x, z)
            if key not in self.visited:
                self.visited[key] = ((key[0] + .5) * self.cell_m, (key[1] + .5) * self.cell_m)
                self.novel += 1
                if len(self.visited) > self.max_cells:
                    self.visited.popitem(last=False)
            if self._last is not None:
                self._distance += math.dist(self._last, (x, z))
            self._last = x, z
            pose_key = (*key, round(math.remainder(yaw, math.tau) / (math.pi / 4)))
            previous = self._poses.get(pose_key)
            loop = bool(previous and self._distance - previous[0] >= 1.5 and self.novel == previous[1])
            if previous is None or loop or self.novel != previous[1]:
                self._poses[pose_key] = self._distance, self.novel
                self._poses.move_to_end(pose_key)
            if len(self._poses) > 4096:
                self._poses.popitem(last=False)
            if loop:
                self.loops += 1
            return loop

    def targets(self, snapshot, grid):
        """Eligible frontier cells; revision/hit-count changes do not forget failures."""
        geometry = (snapshot.origin, grid.cells.shape, snapshot.floor_y, hash(grid.cells.tobytes()))
        with self._lock:
            if self.session is None:
                self._session(snapshot.session)
            if snapshot.session != self.session:
                return np.ones(grid.cells.shape, bool)
            if geometry != self._geometry:
                self.failed.clear()
                self._geometry = geometry
            excluded = tuple(self.visited.values()) + tuple(self.failed.values())
        mask = np.ones(grid.cells.shape, bool)
        radius = max(.30, self.cell_m)
        reach = math.ceil(radius / grid.cell_m)
        for x, z in excluded:
            r, c = grid.cell_of(x, z)
            r0, r1 = max(0, r - reach), min(grid.height, r + reach + 1)
            c0, c1 = max(0, c - reach), min(grid.width, c + reach + 1)
            if r0 >= r1 or c0 >= c1:
                continue
            rr, cc = np.ogrid[r0:r1, c0:c1]
            xx = grid.origin[0] + (cc + .5) * grid.cell_m
            zz = grid.origin[1] + (rr + .5) * grid.cell_m
            mask[r0:r1, c0:c1] &= (xx - x) ** 2 + (zz - z) ** 2 > radius ** 2
        return mask

    def reject(self, session, target):
        if target is None:
            return
        with self._lock:
            if session != self.session:
                return
            self.failed[self._key(*target)] = tuple(target)
            if len(self.failed) > 128:
                self.failed.popitem(last=False)

    def stats(self):
        with self._lock:
            return dict(session=self.session, visited_cells=len(self.visited),
                        novel_cells=self.novel, failed_targets=len(self.failed), loop_replans=self.loops)
