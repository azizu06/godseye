import { expect, test } from "@playwright/test";
import { SurfaceTiles } from "../src/surfaceTiles";
import type { SurfacePatch } from "../src/surfaceTypes";

const triangle = (x = 0, color = 0.5): SurfacePatch => ({
  id: "observed",
  positions: new Float32Array([
    x + 0.1,
    0.1,
    0.1,
    x + 0.8,
    0.1,
    0.1,
    x + 0.1,
    0.8,
    0.1,
  ]),
  colors: new Float32Array(9).fill(color),
  indices: new Uint32Array([0, 1, 2]),
});

test("observed mesh updates only touched tiles and keeps distant coverage", () => {
  const map = new SurfaceTiles();
  const first = map.add(triangle());
  expect(first).toHaveLength(1);
  expect(map.add(triangle())).toEqual([]);
  const distant = map.add(triangle(10));
  expect(distant).toHaveLength(1);
  expect(distant[0].id).not.toBe(first[0].id);
  const updated = map.add(triangle(0, 0.9));
  expect(updated).toHaveLength(1);
  expect(updated[0].id).toBe(first[0].id);
  expect(updated[0].colors![0]).toBeCloseTo(0.9);
  expect(map.triangles).toBe(2);
  expect(updated[0].positions).toEqual(triangle().positions);
});

test("surface budgets preserve old triangles and still allow refinements at capacity", () => {
  const map = new SurfaceTiles(1, 3);
  map.add(triangle());
  expect(map.add(triangle(10))).toEqual([]);
  expect(map.capacity).toBe(true);
  expect(map.triangles).toBe(1);
  expect(map.add(triangle(0, 1))[0].colors![0]).toBe(1);
  expect(() =>
    map.add({ ...triangle(), indices: new Uint32Array([0, 1, 4]) }),
  ).toThrow();
  expect(map.triangles).toBe(1);
});
