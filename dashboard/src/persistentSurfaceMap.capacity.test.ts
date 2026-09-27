import { expect, it } from "vitest";
import { PersistentSurfaceMap } from "./persistentSurfaceMap";
import type { SurfacePatch } from "./surfaceTypes";

// Deterministic replay of a saturated handheld room scan at production budgets.
// Each view is a 64 x 48 depth grid at 1.5 cm, like a refined capture frame,
// with millimetre relief and per-vertex color texture so planar compression
// cannot shrink it. Revisits are shifted, as with real tracking noise.
let seed = 12345;
const random = () => (seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32;
type Surface = (u: number, v: number, depth: number) => number[];
const room: Surface[] = [
  (u, v, d) => [d, v, u],
  (u, v, d) => [5 + d, v, u],
  (u, v, d) => [u, v, d],
  (u, v, d) => [u, v, 4 + d],
  (u, v, d) => [u, d, v],
];
const ceiling: Surface = (u, v, d) => [u, 2.5 + d, v];
function view(surface: Surface, u0: number, v0: number): SurfacePatch {
  const cols = 64,
    rows = 48,
    step = 0.015;
  const positions: number[] = [],
    colors: number[] = [],
    indices: number[] = [];
  for (let r = 0; r < rows; r++)
    for (let c = 0; c < cols; c++) {
      const u = u0 + c * step,
        v = v0 + r * step;
      const depth =
        0.012 * Math.sin(u * 40) * Math.cos(v * 37) + (random() - 0.5) * 0.004;
      positions.push(...surface(u, v, depth));
      colors.push(random(), random(), random());
    }
  for (let r = 0; r < rows - 1; r++)
    for (let c = 0; c < cols - 1; c++) {
      const a = r * cols + c,
        b = a + cols;
      indices.push(a, b, a + 1, a + 1, b, b + 1);
    }
  return {
    id: `${u0},${v0}`,
    positions: new Float32Array(positions),
    colors: new Float32Array(colors),
    indices: new Uint32Array(indices),
  };
}
const centroids = (patch: SurfacePatch) => {
  const result: number[][] = [];
  for (let i = 0; i < patch.indices.length; i += 3)
    result.push(
      [0, 1, 2].map(
        (axis) =>
          [0, 1, 2].reduce(
            (sum, j) => sum + patch.positions[patch.indices[i + j] * 3 + axis],
            0,
          ) / 3,
      ),
    );
  return result;
};
// A view counts as retained when some retained face centroid lies within its
// bounds (plus 2 cm); coarse faces never extend beyond observed support.
const bounds = (patch: SurfacePatch) =>
  [0, 1, 2].map((axis) => {
    let min = Infinity,
      max = -Infinity;
    for (let i = axis; i < patch.positions.length; i += 3) {
      min = Math.min(min, patch.positions[i]);
      max = Math.max(max, patch.positions[i]);
    }
    return [min - 0.02, max + 0.02];
  });
const retains = (faces: number[][], patch: SurfacePatch) => {
  const box = bounds(patch);
  return faces.some((face) =>
    box.every(([min, max], axis) => face[axis] >= min && face[axis] <= max),
  );
};

it("keeps growing a saturated room scan within bounded memory and work", () => {
  const map = new PersistentSurfaceMap();
  const earlier: SurfacePatch[] = [];
  let slowest = 0,
    coarsened = 0;
  const add = (patch: SurfacePatch) => {
    const started = performance.now();
    const result = map.add(patch);
    slowest = Math.max(slowest, performance.now() - started);
    if (result.reset) coarsened++;
    expect(map.triangleCount).toBeLessThanOrEqual(1_000_000);
    expect(map.snapshot()!.positions.length / 3).toBeLessThanOrEqual(500_000);
  };
  // Saturate the vertex budget with the walls and floor, then keep scanning.
  for (let i = 0; i < 190; i++) {
    const patch = view(room[i % room.length], random() * 3, random() * 1.8);
    if (i < 150) earlier.push(patch);
    add(patch);
  }
  expect(coarsened).toBeGreaterThan(1);
  // A later, never-before-scanned area must still enter the retained map.
  const later = [0.5, 1.5, 2.5].map((u) => view(ceiling, u, 1.5));
  later.forEach(add);
  const snapshot = map.snapshot()!;
  const faces = centroids(snapshot);
  for (const patch of later) expect(retains(faces, patch)).toBe(true);
  // Every early view keeps observed geometry after capacity recovery.
  for (const patch of earlier) expect(retains(faces, patch)).toBe(true);
  // Retained geometry stays finite, indexed and non-degenerate.
  for (const value of snapshot.positions)
    expect(Number.isFinite(value)).toBe(true);
  const vertices = snapshot.positions.length / 3;
  for (let i = 0; i < snapshot.indices.length; i += 3) {
    const [a, b, c] = snapshot.indices.subarray(i, i + 3);
    expect(Math.max(a, b, c)).toBeLessThan(vertices);
    expect(new Set([a, b, c]).size).toBe(3);
  }
  // Capacity recovery amortizes work below the 8-second coarsening deadline.
  expect(slowest).toBeLessThan(8_000);
  expect(map.cellM).toBeGreaterThan(0);
}, 300_000);
