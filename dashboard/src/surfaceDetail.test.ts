import { expect, it } from "vitest";
import { bakeSurfaceColors } from "./surfaceColor";
import { PersistentSurfaceMap } from "./persistentSurfaceMap";
import type { SurfacePatch } from "./surfaceTypes";
const quad: SurfacePatch = {
  id: "observed-wall",
  positions: new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0]),
  uvs: new Float32Array([0, 1, 1, 1, 0, 0, 1, 0]),
  indices: new Uint32Array([0, 1, 2, 1, 3, 2]),
};
function pixels(mark = true) {
  const width = 64,
    height = 64;
  const data = new Uint8ClampedArray(width * height * 4).fill(255);
  if (mark)
    for (let y = 16; y < 48; y++)
      for (let x = 16; x < 48; x++) {
        const i = (y * width + x) * 4;
        data[i] = data[i + 1] = data[i + 2] = 0;
      }
  return { width, height, data };
}
function sample(patch: SurfacePatch, x: number, y: number) {
  for (let i = 0; i < patch.indices.length; i += 3) {
    const ids = [...patch.indices.slice(i, i + 3)];
    const [a, b, c] = ids.map((id) => [
      ...patch.positions.slice(id * 3, id * 3 + 2),
    ]);
    const area = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    const w = [
      ((b[0] - x) * (c[1] - y) - (b[1] - y) * (c[0] - x)) / area,
      ((c[0] - x) * (a[1] - y) - (c[1] - y) * (a[0] - x)) / area,
    ];
    w.push(1 - w[0] - w[1]);
    if (w.every((v) => v >= -1e-6))
      return ids.reduce((sum, id, j) => sum + w[j] * patch.colors![id * 3], 0);
  }
  return null;
}
it("retains high-contrast image detail between coarse vertices after persistent fusion", () => {
  const baked = bakeSurfaceColors(quad, pixels());
  const map = new PersistentSurfaceMap();
  map.add(baked);
  const retained = map.snapshot()!;
  expect(sample(retained, 0.5, 0.5)).toBeLessThan(0.1);
  expect(sample(retained, 0.05, 0.05)).toBeGreaterThan(0.9);
  expect(retained.positions.length / 3).toBeGreaterThan(4);
});
it("keeps a plain wall coarse and never fills unobserved triangles", () => {
  expect(bakeSurfaceColors(quad, pixels(false)).positions).toEqual(
    quad.positions,
  );
  const half = bakeSurfaceColors(
    { ...quad, indices: new Uint32Array([0, 1, 2]) },
    pixels(),
  );
  expect(sample(half, 0.8, 0.8)).toBeNull();
  for (let i = 2; i < half.positions.length; i += 3)
    expect(half.positions[i]).toBe(0);
  expect(half.positions.length / 3).toBeLessThanOrEqual(6144);
});

it("preserves a stripe across the shared edge and leaves source buffers unchanged", () => {
  const image = pixels(false);
  for (let y = 0; y < 64; y++)
    for (let x = 28; x < 36; x++) {
      const i = (y * 64 + x) * 4;
      image.data[i] = image.data[i + 1] = image.data[i + 2] = 0;
    }
  const before = structuredClone(quad);
  const result = bakeSurfaceColors(quad, image);
  for (const y of [0.1, 0.3, 0.5, 0.7, 0.9])
    expect(sample(result, 0.5, y)).toBeLessThan(0.1);
  expect(quad).toEqual(before);
  let area = 0;
  for (let i = 0; i < result.indices.length; i += 3) {
    const [a, b, c] = [...result.indices.slice(i, i + 3)].map((id) => [
      ...result.positions.slice(id * 3, id * 3 + 2),
    ]);
    const twice = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    expect(twice).toBeGreaterThan(0);
    area += twice / 2;
  }
  expect(area).toBeCloseTo(1, 7);
});
it("keeps a smooth linear-light gradient coarse", () => {
  const image = pixels(false);
  for (let y = 0; y < 64; y++)
    for (let x = 0; x < 64; x++) {
      const value = x / 63;
      const encoded =
        value <= 0.0031308 ? 12.92 * value : 1.055 * value ** (1 / 2.4) - 0.055;
      const i = (y * 64 + x) * 4;
      image.data[i] =
        image.data[i + 1] =
        image.data[i + 2] =
          Math.round(encoded * 255);
    }
  expect(bakeSurfaceColors(quad, image).positions.length).toBe(12);
});
it("bounds busy texture refinement without dropping observed coverage", () => {
  const positions: number[] = [],
    uvs: number[] = [],
    indices: number[] = [];
  const cols = 64,
    rows = 48;
  for (let y = 0; y < rows; y++)
    for (let x = 0; x < cols; x++) {
      positions.push(x / (cols - 1), y / (rows - 1), 0);
      uvs.push(x / (cols - 1), 1 - y / (rows - 1));
    }
  for (let y = 0; y < rows - 1; y++)
    for (let x = 0; x < cols - 1; x++) {
      const a = y * cols + x,
        b = a + cols;
      indices.push(a, a + 1, b, a + 1, b + 1, b);
    }
  const width = 1024,
    height = 768,
    data = new Uint8ClampedArray(width * height * 4).fill(255);
  for (let y = 0; y < height; y++)
    for (let x = 0; x < width; x++) {
      const v = ((Math.floor(x / 7) + Math.floor(y / 7)) % 2) * 255,
        i = (y * width + x) * 4;
      data[i] = data[i + 1] = data[i + 2] = v;
    }
  const result = bakeSurfaceColors(
    {
      id: "busy",
      positions: new Float32Array(positions),
      uvs: new Float32Array(uvs),
      indices: new Uint32Array(indices),
    },
    { width, height, data },
  );
  expect(result.positions.length / 3).toBeLessThanOrEqual(6144);
  expect(result.positions.length / 3).toBeGreaterThan(3072);
  let area = 0;
  for (let i = 0; i < result.indices.length; i += 3) {
    const [a, b, c] = [...result.indices.slice(i, i + 3)].map((id) => [
      ...result.positions.slice(id * 3, id * 3 + 2),
    ]);
    const twice = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    expect(twice).toBeGreaterThan(0);
    area += twice / 2;
  }
  expect(area).toBeCloseTo(1, 5);
  expect([...result.positions, ...result.colors!].every(Number.isFinite)).toBe(
    true,
  );
});

it("preserves small-scale observed color and footprint until the map needs coarsening", () => {
  for (const scale of [0.04, 0.06, 0.1]) {
    const small = {
      ...quad,
      positions: Float32Array.from(quad.positions, (v) => v * scale),
    };
    const map = new PersistentSurfaceMap();
    map.add(bakeSurfaceColors(small, pixels()));
    const retained = map.snapshot()!;
    expect(sample(retained, scale * 0.5, scale * 0.5)).toBeLessThan(0.1);
    for (let y = 0; y < 20; y++)
      for (let x = 0; x < 20; x++)
        expect(
          sample(retained, (scale * (x + 0.5)) / 20, (scale * (y + 0.5)) / 20),
        ).not.toBeNull();
  }
});
