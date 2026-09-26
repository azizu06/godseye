import { expect, test } from "@playwright/test";
import { PointCloudStore, MAX_POINTS } from "../src/pointCloud";
import { parseWalls, wallVertices } from "../src/walls";

const identity = { session_id: "wall-test", map_epoch: 1 };
const wall = {
  id: "wall-a",
  corners: [-1, 0, -2, 1, 0, -2, 1, 2, -2, -1, 2, -2],
};
const snapshot = (t = 1, walls = [wall]) => ({
  version: 2,
  type: "walls",
  available: true,
  ...identity,
  t_capture: t,
  walls,
});
function points(cloud: PointCloudStore, positions: number[], id = 1) {
  cloud.ingest({
    version: 1,
    type: "points",
    ...identity,
    chunk_id: id,
    t_capture: id,
    positions,
    colors: positions.map(() => 0.5),
  });
}

test("confirmed walls reclaim point capacity while foreground objects and wall borders stay detailed", () => {
  const cloud = new PointCloudStore(4);
  points(cloud, [0, 1, -2, 0.5, 1, -1.99, 0, 1, -1.8, 1, 1, -2]);
  expect(cloud.count).toBe(4);
  cloud.updateWalls(snapshot());
  expect(cloud.pruneWallPoints(1)).toBe(true); // Work is split across bounded batches.
  while (cloud.pruneWallPoints()) {
    /* Drain remaining work. */
  }
  expect(cloud.count).toBe(2);
  const retained = [...cloud.positions.slice(0, 6)];
  expect(retained).toContain(1);
  expect(retained.some((v) => Math.abs(v + 1.8) < 1e-6)).toBe(true);
  points(cloud, [0, 1, -2, -0.5, 1, -2.01, 0.2, 1, -1.5, 0.4, 1, -1.5], 2);
  expect(cloud.count).toBe(4);
  expect(cloud.evicted).toBe(0); // Freed slots were used by two non-wall observations.
  points(cloud, [0.2, 1, -1.5], 3);
  expect(cloud.count).toBe(4); // Compaction preserves the voxel index.
});

test("walls update as full snapshots and never cross map boundaries", () => {
  const cloud = new PointCloudStore(4);
  cloud.announce({ version: 1, type: "objects", ...identity });
  expect(cloud.updateWalls(snapshot())).toBe(true);
  expect(cloud.bounds()?.center).toEqual([0, 1, -2]);
  expect(cloud.updateWalls({ ...snapshot(2), session_id: "old-map" })).toBe(
    false,
  );
  expect(cloud.updateWalls(snapshot(0))).toBe(false);
  expect(cloud.updateWalls(snapshot(2, []))).toBe(true);
  expect(cloud.bounds()).toBeNull();
  cloud.updateWalls(snapshot(3));
  cloud.announce({
    version: 1,
    type: "objects",
    session_id: "next-map",
    map_epoch: 1,
  });
  expect(cloud.walls).toEqual([]);
  expect(cloud.updateWalls(snapshot(4))).toBe(false);
});

test("rectangles draw as two solid triangles and malformed rectangles cannot remove points", () => {
  expect(wallVertices([wall]).length).toBe(18);
  expect(parseWalls(snapshot())).not.toBeNull();
  for (const corners of [
    [1, 2],
    Array(12).fill(0),
    [...wall.corners.slice(0, 11), NaN],
    [0, 0, 0, 2, 0, 0, 2, 0, 2, 0, 0, 2],
  ]) {
    expect(parseWalls(snapshot(2, [{ ...wall, corners }]))).toBeNull();
  }
});

test("point buffer retains two million distinct samples and uploads appended ranges only", () => {
  const cloud = new PointCloudStore();
  expect(MAX_POINTS).toBe(2_000_000);
  const colors = Array(7500).fill(0.5);
  for (let offset = 0; offset < MAX_POINTS; offset += 2500) {
    const positions = [];
    for (let i = offset; i < offset + 2500; i++)
      positions.push((i % 2000) * 0.02, Math.floor(i / 2000) * 0.02, 0);
    cloud.ingest({
      version: 1,
      type: "points",
      ...identity,
      chunk_id: offset + 1,
      positions,
      colors,
    });
    expect(cloud.takeUpdateRanges()).toEqual([
      { start: offset * 3, count: 7500 },
    ]);
  }
  expect(cloud.count).toBe(2_000_000);
  expect(cloud.evicted).toBe(0);
  points(cloud, [90, 90, 90], MAX_POINTS + 1);
  expect(cloud.count).toBe(2_000_000);
  expect(cloud.evicted).toBe(1);
});
