import { describe, expect, it, vi } from "vitest";
import { PersistentSurfaceMap } from "./persistentSurfaceMap";
import type { SurfacePatch } from "./surfaceTypes";

function plane(
  x: number,
  z = 0,
  size = 1,
  steps = 4,
  color = [0.7, 0.2, 0.1],
): SurfacePatch {
  const positions: number[] = [],
    indices: number[] = [],
    colors: number[] = [];
  for (let row = 0; row <= steps; row++)
    for (let col = 0; col <= steps; col++) {
      positions.push(x + (col * size) / steps, 0, z + (row * size) / steps);
      colors.push(...color);
    }
  for (let row = 0; row < steps; row++)
    for (let col = 0; col < steps; col++) {
      const a = row * (steps + 1) + col,
        b = a + 1,
        c = a + steps + 1,
        d = c + 1;
      indices.push(a, c, b, b, c, d);
    }
  return {
    id: `${x},${z}`,
    positions: new Float32Array(positions),
    indices: new Uint32Array(indices),
    colors: new Float32Array(colors),
  };
}
function validate(patch: SurfacePatch) {
  expect(patch.positions.length % 3).toBe(0);
  expect(patch.colors!.length).toBe(patch.positions.length);
  expect(patch.indices.length % 3).toBe(0);
  for (const value of [...patch.positions, ...patch.colors!])
    expect(Number.isFinite(value)).toBe(true);
  for (const index of patch.indices)
    expect(index).toBeLessThan(patch.positions.length / 3);
}
const covers = (patch: SurfacePatch, x: number, z = 0, size = 1) =>
  [...patch.indices].some(
    (i) =>
      patch.positions[i * 3] >= x - 0.001 &&
      patch.positions[i * 3] <= x + size + 0.001 &&
      patch.positions[i * 3 + 2] >= z - 0.001 &&
      patch.positions[i * 3 + 2] <= z + size + 0.001,
  );

describe("persistent surface map", () => {
  it("retains more than 24 distinct views and their original linear colors", () => {
    const map = new PersistentSurfaceMap();
    expect(map.snapshot()).toBeNull();
    for (let i = 0; i < 40; i++)
      map.add(
        plane(i * 3, 0, 1, 4, i === 0 ? [0.9, 0.1, 0.05] : [0.1, 0.4, 0.8]),
      );
    const patch = map.snapshot()!;
    for (let i = 0; i < 40; i++) expect(covers(patch, i * 3)).toBe(true);
    const original = [...patch.indices].find(
      (i) => patch.positions[i * 3] < 2,
    )!;
    expect([...patch.colors!.slice(original * 3, original * 3 + 3)]).toEqual([
      ...new Float32Array([0.9, 0.1, 0.05]),
    ]);
    validate(patch);
  });
  it("deduplicates repeated views and preserves immutable snapshots", () => {
    const map = new PersistentSurfaceMap();
    const source = plane(0);
    map.add(source);
    const snapshot = map.snapshot()!,
      saved = [...snapshot.positions],
      count = map.triangleCount;
    for (let i = 0; i < 100; i++) map.add({ ...source, id: `revisit-${i}` });
    expect(map.triangleCount).toBe(count);
    map.add(plane(5));
    expect([...snapshot.positions]).toEqual(saved);
    expect(snapshot.indices.length).toBe(count * 3);
    expect(covers(map.snapshot()!, 0)).toBe(true);
  });
  it("coarsens globally under a budget while keeping far old areas and thin patches", () => {
    const map = new PersistentSurfaceMap({
      maxVertices: 160,
      maxTriangles: 160,
      initialCellM: 0.02,
    });
    for (let i = 0; i < 12; i++) {
      const detailed = plane(i * 5, 0, 1, 15);
      // Sharp color detail must retain dense geometry, exercising the adaptive
      // fallback even when uniform planes now compress before accumulation.
      for (let vertex = 0; vertex < detailed.colors!.length / 3; vertex++)
        detailed.colors![vertex * 3 + 2] = vertex % 2 ? 0.8 : 0.1;
      map.add(detailed);
    }
    map.add(plane(-10, 0, 0.005, 2, [0.2, 0.8, 0.1]));
    const patch = map.snapshot()!;
    expect(map.cellM).toBeGreaterThan(0.02);
    expect(map.triangleCount).toBeLessThanOrEqual(160);
    expect(patch.positions.length / 3).toBeLessThanOrEqual(160);
    for (let i = 0; i < 12; i++) expect(covers(patch, i * 5)).toBe(true);
    expect(covers(patch, -10, 0, 0.005)).toBe(true);
    const oldColor = [...patch.indices].find(
      (i) => patch.positions[i * 3] >= 0 && patch.positions[i * 3] < 2,
    )!;
    expect([...patch.colors!.slice(oldColor * 3, oldColor * 3 + 3)]).toEqual([
      ...new Float32Array([0.7, 0.2, 0.1]),
    ]);
    validate(patch);
    for (let i = 0; i < patch.indices.length; i += 3) {
      const xs = [0, 1, 2].map(
        (j) => patch.positions[patch.indices[i + j] * 3],
      );
      expect(Math.max(...xs) - Math.min(...xs)).toBeLessThanOrEqual(1.001);
    }
  });
  it("does not erase a plane smaller than the initial clustering cell", () => {
    const map = new PersistentSurfaceMap();
    map.add(plane(0, 0, 0.001, 2));
    expect(map.triangleCount).toBeGreaterThan(0);
    validate(map.snapshot()!);
  });
  it("fails atomically when the budget cannot represent additional disconnected regions", () => {
    const map = new PersistentSurfaceMap({ maxTriangles: 2, maxVertices: 6 });
    map.add(plane(0));
    map.add(plane(10));
    const before = map.snapshot()!;
    expect(() => map.add(plane(20))).toThrow(/capacity|budget/i);
    expect(map.snapshot()).toBe(before);
    expect(covers(before, 0)).toBe(true);
    expect(covers(before, 10)).toBe(true);
  });
  it("rejects malformed colors, indices and coordinates without damaging the map", () => {
    const map = new PersistentSurfaceMap();
    map.add(plane(0));
    const before = map.snapshot();
    const invalid = plane(3);
    invalid.positions[0] = NaN;
    expect(() => map.add(invalid)).toThrow();
    expect(() =>
      map.add({ ...plane(3), indices: new Uint32Array([0, 1, 999]) }),
    ).toThrow();
    expect(() => map.add({ ...plane(3), colors: undefined })).toThrow();
    expect(map.snapshot()).toBe(before);
  });
});

it("does not fill an unobserved hole inside a connected scanned floor when coarsening", () => {
  const ring = plane(0, 0, 3, 24);
  const indices: number[] = [];
  for (let i = 0; i < ring.indices.length; i += 3) {
    const triangle = [...ring.indices.slice(i, i + 3)];
    const x = triangle.reduce((n, j) => n + ring.positions[j * 3], 0) / 3;
    const z = triangle.reduce((n, j) => n + ring.positions[j * 3 + 2], 0) / 3;
    if (!(x > 1 && x < 2 && z > 1 && z < 2)) indices.push(...triangle);
  }
  ring.indices = new Uint32Array(indices);
  const map = new PersistentSurfaceMap({ maxVertices: 16, maxTriangles: 8 });
  map.add(ring);
  const patch = map.snapshot()!;
  const xs = [...patch.indices].map((i) => patch.positions[i * 3]),
    zs = [...patch.indices].map((i) => patch.positions[i * 3 + 2]);
  expect(Math.min(...xs)).toBeLessThan(0.5);
  expect(Math.max(...xs)).toBeGreaterThan(2.05);
  expect(Math.min(...zs)).toBeLessThan(0.5);
  expect(Math.max(...zs)).toBeGreaterThan(2.05);
  const inside = (
    x: number,
    z: number,
    a: number[],
    b: number[],
    c: number[],
  ) => {
    const cross = (p: number[], q: number[]) =>
      (q[0] - p[0]) * (z - p[1]) - (q[1] - p[1]) * (x - p[0]);
    const d = [cross(a, b), cross(b, c), cross(c, a)];
    return d.every((v) => v > 1e-7) || d.every((v) => v < -1e-7);
  };
  for (let x = 1.1; x < 2; x += 0.2)
    for (let z = 1.1; z < 2; z += 0.2)
      for (let i = 0; i < patch.indices.length; i += 3) {
        const p = [0, 1, 2].map((j) => {
          const k = patch.indices[i + j] * 3;
          return [patch.positions[k], patch.positions[k + 2]];
        });
        expect(inside(x, z, p[0], p[1], p[2])).toBe(false);
      }
});

it("fails an expired coarsening job atomically while retaining the prior map", () => {
  const map = new PersistentSurfaceMap({
    maxVertices: 500,
    maxTriangles: 1000,
  });
  map.add(plane(0));
  const prior = map.snapshot();
  const detailed = plane(2, 0, 1, 30);
  for (let i = 0; i < detailed.colors!.length; i++)
    detailed.colors![i] = i % 2 ? 0.1 : 0.9;
  let calls = 0;
  const clock = vi
    .spyOn(performance, "now")
    .mockImplementation(() => (++calls <= 2 ? 0 : 9001));
  try {
    expect(() => map.add(detailed)).toThrow(/capacity.*processing time/);
    expect(calls).toBeGreaterThanOrEqual(3);
    expect(map.snapshot()).toBe(prior);
    expect(map.cellM).toBe(0);
  } finally {
    clock.mockRestore();
  }
  // A bounded failure does not poison the map or its next small integration.
  map.add(plane(4));
  expect(covers(map.snapshot()!, 4)).toBe(true);
});
