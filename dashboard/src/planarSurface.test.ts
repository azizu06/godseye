import { describe, expect, it } from "vitest";
import { Euler, Quaternion, Vector3 } from "three";
import { simplifyPlanarPatch } from "./planarSurface";
import type { SurfacePatch } from "./surfaceTypes";
import { PersistentSurfaceMap } from "./persistentSurfaceMap";

function grid(
  options: {
    steps?: number;
    noise?: number;
    tilted?: boolean;
    gradient?: boolean;
    checker?: boolean;
    hole?: boolean;
    reverse?: boolean;
    shift?: number;
    crease?: boolean;
  } = {},
): SurfacePatch {
  const {
    steps = 20,
    noise = 0,
    tilted = false,
    gradient = false,
    checker = false,
    hole = false,
    reverse = false,
    shift = 0,
    crease = false,
  } = options;
  const p: number[] = [],
    c: number[] = [],
    indices: number[] = [];
  for (let row = 0; row <= steps; row++)
    for (let col = 0; col <= steps; col++) {
      const x = (2 * col) / steps,
        z = (2 * row) / steps,
        y =
          noise * Math.sin(col * 0.73 + row * 0.41) +
          (crease ? Math.max(0, x - 1) * 0.4 : 0);
      if (tilted)
        p.push((x - y) / Math.sqrt(2) + shift, (x + y) / Math.sqrt(2), z + 3);
      else p.push(x + shift, y, z);
      const bright = (Math.floor(row / 2) + Math.floor(col / 2)) % 2;
      c.push(
        ...(gradient
          ? [x / 2, 0.3, z / 2]
          : checker
            ? [bright, 1 - bright, 0.2]
            : [0.6, 0.45, 0.3]),
      );
    }
  for (let row = 0; row < steps; row++)
    for (let col = 0; col < steps; col++) {
      const x = (2 * (col + 0.5)) / steps,
        z = (2 * (row + 0.5)) / steps;
      if (hole && x > 0.8 && x < 1.2 && z > 0.8 && z < 1.2) continue;
      const a = row * (steps + 1) + col,
        b = a + 1,
        d = a + steps + 1,
        e = d + 1;
      indices.push(...(reverse ? [b, d, a, e, d, b] : [a, d, b, b, d, e]));
    }
  return {
    id: "plane",
    positions: new Float32Array(p),
    colors: new Float32Array(c),
    indices: new Uint32Array(indices),
  };
}
function sample(patch: SurfacePatch, x: number, z: number): number[] | null {
  for (let i = 0; i < patch.indices.length; i += 3) {
    const ids = [...patch.indices.slice(i, i + 3)],
      p = ids.map((j) => [...patch.positions.slice(j * 3, j * 3 + 3)]);
    const [a, b, c] = p,
      den = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2]);
    if (Math.abs(den) < 1e-12) continue;
    const u = ((b[2] - c[2]) * (x - c[0]) + (c[0] - b[0]) * (z - c[2])) / den,
      v = ((c[2] - a[2]) * (x - c[0]) + (a[0] - c[0]) * (z - c[2])) / den,
      w = 1 - u - v;
    if (Math.min(u, v, w) < -1e-6) continue;
    return [0, 1, 2].map((channel) =>
      [u, v, w].reduce(
        (sum, weight, j) => sum + weight * patch.colors![ids[j] * 3 + channel],
        0,
      ),
    );
  }
  return null;
}

describe("observed planar compression", () => {
  it("turns a dense observed rectangle into a colored quad with compact buffers", () => {
    const input = grid(),
      out = simplifyPlanarPatch(input);
    expect(out.positions.length / 3).toBe(4);
    expect(out.indices.length / 3).toBe(2);
    expect(out.colors!.length).toBe(out.positions.length);
    expect(sample(out, 0.1, 0.1)).not.toBeNull();
    expect(sample(out, 1.9, 1.9)).not.toBeNull();
    expect(input.positions.length / 3).toBe(441);
  });
  it("handles arbitrary orientation, reversed order/winding and small measured depth noise", () => {
    for (const reverse of [false, true]) {
      const out = simplifyPlanarPatch(
        grid({ tilted: true, noise: 0.002, reverse }),
      );
      expect(out.positions.length / 3).toBeLessThan(100);
      for (let i = 0; i < out.positions.length; i += 3)
        expect(Math.abs(out.positions[i] - out.positions[i + 1])).toBeLessThan(
          0.008 * Math.sqrt(2),
        );
    }
  });
  it("preserves an observed hole and coverage on every side", () => {
    const out = simplifyPlanarPatch(grid({ hole: true }));
    expect(out.positions.length / 3).toBeLessThan(40);
    expect(sample(out, 1, 1)).toBeNull();
    for (const [x, z] of [
      [0.1, 1],
      [1.9, 1],
      [1, 0.1],
      [1, 1.9],
    ])
      expect(sample(out, x, z)).not.toBeNull();
  });
  it("preserves a linear color gradient but refuses to flatten sharp checkerboard detail", () => {
    const gradient = simplifyPlanarPatch(grid({ gradient: true }));
    expect(gradient.positions.length / 3).toBe(4);
    for (const [x, z] of [
      [0.3, 0.7],
      [1.3, 1.7],
    ]) {
      const color = sample(gradient, x, z)!;
      expect(color[0]).toBeCloseTo(x / 2, 5);
      expect(color[2]).toBeCloseTo(z / 2, 5);
    }
    const detailed = grid({ checker: true }),
      out = simplifyPlanarPatch(detailed);
    expect(out.positions.length / 3).toBeGreaterThan(100);
    for (const [x, z] of [
      [0.05, 0.05],
      [0.25, 0.05],
      [0.35, 0.45],
    ]) {
      const actual = sample(out, x, z)!,
        expected = sample(detailed, x, z)!;
      for (let c = 0; c < 3; c++)
        expect(Math.abs(actual[c] - expected[c])).toBeLessThanOrEqual(0.04);
    }
  });
  it("does not flatten a crease or high residual depth into one guessed plane", () => {
    const crease = grid({ crease: true }),
      out = simplifyPlanarPatch(crease);
    expect(
      Math.max(...out.positions.filter((_, i) => i % 3 === 1)),
    ).toBeCloseTo(0.4, 5);
    expect(sample(out, 0.5, 1)).not.toBeNull();
    expect(sample(out, 1.5, 1)).not.toBeNull();
    const noisy = grid({ noise: 0.05 });
    expect(simplifyPlanarPatch(noisy).positions.length / 3).toBeGreaterThan(
      100,
    );
  });
  it("actually frees persistent vertices and bounds growth over shifted noisy revisits", () => {
    const map = new PersistentSurfaceMap();
    for (let i = 0; i < 30; i++)
      map.add(
        grid({
          steps: 20 + (i % 3),
          noise: 0.001 * Math.sin(i),
          shift: 0.002 * Math.sin(i),
        }),
      );
    expect(map.snapshot()!.positions.length / 3).toBeLessThan(600);
    expect(map.triangleCount).toBeLessThan(600);
    expect(sample(map.snapshot()!, 1, 1)).not.toBeNull();
  });
});

it("bounds the actual rendered color after handling out-of-gamut fits", () => {
  const input = grid(),
    red = [
      0, 0, 0.0172431779, 0, 0.014030716, 0.0069310169, 0.0001765015,
      0.0104354819, 0.0598484572, 0.0625435847, 0.0395676047, 0.0664439784,
      0.0824519334, 0.0937751117, 0.0770514881, 0.1075184663, 0.124442885,
      0.1124987597, 0.1372612375, 0.1206798316, 0.1259978586,
    ];
  for (let i = 0; i < input.colors!.length / 3; i++)
    input.colors!.set([red[i % 21], 0.4, 0.4], i * 3);
  const out = simplifyPlanarPatch(input);
  for (let col = 0; col <= 20; col++)
    expect(
      Math.abs(sample(out, col / 10, 1)![0] - red[col]),
    ).toBeLessThanOrEqual(0.04);
});

it("preserves the color bound through coincident-seam integration", () => {
  const input = grid(),
    red = [
      0.2960656767, 0.2946907372, 0.3134755042, 0.2877708376, 0.2875448289,
      0.2905045794, 0.329243642, 0.2873075721, 0.2905430398, 0.3274091101,
      0.3031331424, 0.507970269, 0.5061653651, 0.5184248398, 0.4806728512,
      0.491792615, 0.5166627194, 0.4871325058, 0.5083073704, 0.4999396902,
      0.5069394444,
    ];
  for (let i = 0; i < input.colors!.length / 3; i++)
    input.colors!.set([red[i % 21], 0.4, 0.4], i * 3);
  const map = new PersistentSurfaceMap();
  map.add(input);
  for (let col = 0; col <= 20; col++)
    expect(
      Math.abs(sample(map.snapshot()!, col / 10, 1)![0] - red[col]),
    ).toBeLessThanOrEqual(0.04);
});

function footprint(input: SurfacePatch, output: SurfacePatch) {
  for (let x = 0.05; x < 2; x += 0.1)
    for (let z = 0.05; z < 2; z += 0.1)
      expect(sample(output, x, z) !== null).toBe(sample(input, x, z) !== null);
}
function transform(patch: SurfacePatch, rotation: Quaternion): SurfacePatch {
  const positions = new Float32Array(patch.positions.length);
  for (let i = 0; i < positions.length; i += 3)
    new Vector3(
      patch.positions[i],
      patch.positions[i + 1],
      patch.positions[i + 2],
    )
      .applyQuaternion(rotation)
      .toArray(positions, i);
  return { ...patch, positions };
}
it("preserves the complete sampled footprint of vertical and multiply rotated planes", () => {
  const input = grid({ hole: true });
  for (const rotation of [
    new Quaternion().setFromEuler(new Euler(Math.PI / 2, 0, 0)),
    new Quaternion().setFromEuler(new Euler(0.53, 0.91, 0.27)),
  ]) {
    const out = transform(
      simplifyPlanarPatch(transform(input, rotation)),
      rotation.clone().invert(),
    );
    expect(out.positions.length / 3).toBeLessThan(40);
    footprint(input, out);
  }
});
it("preserves concave outlines, narrow slits and disconnected observed islands", () => {
  for (const missing of [
    (x: number, z: number) => x > 1 && z > 1,
    (x: number, z: number) => x > 0.6 && x < 1.4 && z > 0.6,
    (x: number, z: number) => x > 0.9 && x < 1.1 && z > 0.4 && z < 1.6,
    (x: number) => x > 0.8 && x < 1.2,
  ]) {
    const input = grid(),
      indices: number[] = [];
    for (let i = 0; i < input.indices.length; i += 3) {
      const face = [...input.indices.slice(i, i + 3)],
        x = face.reduce((sum, j) => sum + input.positions[j * 3], 0) / 3,
        z = face.reduce((sum, j) => sum + input.positions[j * 3 + 2], 0) / 3;
      if (!missing(x, z)) indices.push(...face);
    }
    input.indices = new Uint32Array(indices);
    const out = simplifyPlanarPatch(input);
    expect(out.positions.length / 3).toBeLessThan(input.positions.length / 3);
    footprint(input, out);
  }
});
it("retains a small high-contrast interior mark at vertices and triangle interiors", () => {
  const input = grid();
  input.colors!.set([1, 0, 0], (10 * 21 + 10) * 3);
  const map = new PersistentSurfaceMap();
  map.add(input);
  const out = map.snapshot()!;
  const check = (x: number, z: number) => {
    const actual = sample(out, x, z),
      expected = sample(input, x, z);
    expect(actual).not.toBeNull();
    for (let c = 0; c < 3; c++)
      expect(Math.abs(actual![c] - expected![c])).toBeLessThanOrEqual(0.040001);
  };
  for (const index of new Set(input.indices))
    check(input.positions[index * 3], input.positions[index * 3 + 2]);
  for (let i = 0; i < input.indices.length; i += 3) {
    const face = [...input.indices.slice(i, i + 3)];
    check(
      face.reduce((sum, j) => sum + input.positions[j * 3], 0) / 3,
      face.reduce((sum, j) => sum + input.positions[j * 3 + 2], 0) / 3,
    );
  }
});
