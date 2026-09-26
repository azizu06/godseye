import { expect, it } from "vitest";
import { bakeSurfaceColors, serializeSurface } from "./surfaceColor";
import type { SurfacePatch } from "./surfaceTypes";
const patch: SurfacePatch = {
  id: "frame",
  positions: new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0]),
  indices: new Uint32Array([0, 1, 2]),
  uvs: new Float32Array([0.25, 0.75, 0.75, 0.75, 0.25, 0.25]),
};
it("bakes top-left red/top-right green/bottom-left blue without flipping or tinting", () => {
  const result = bakeSurfaceColors(patch, {
    width: 2,
    height: 2,
    data: new Uint8ClampedArray([
      255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255, 128, 128, 128, 255,
    ]),
  });
  expect(Array.from(result.colors!)).toEqual([1, 0, 0, 0, 1, 0, 0, 0, 1]);
  expect(result.jpeg).toBeUndefined();
  expect(result.image).toBeUndefined();
});
it("converts gamma-encoded gray to linear color and rejects malformed images", () => {
  const image = {
    width: 1,
    height: 1,
    data: new Uint8ClampedArray([128, 128, 128, 255]),
  };
  expect(bakeSurfaceColors(patch, image).colors![0]).toBeCloseTo(0.21586, 4);
  expect(() =>
    bakeSurfaceColors(patch, { ...image, data: new Uint8ClampedArray() }),
  ).toThrow();
});
it("exports only observed indexed vertices with color and resolution", () => {
  const mesh = {
    ...patch,
    positions: new Float32Array([99, 99, 99, 1, 2, 3, 4, 5, 6, 7, 8, 9]),
    colors: new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]),
    indices: new Uint32Array([1, 2, 3]),
  };
  expect(serializeSurface(mesh, 0.02)).toMatchObject({
    positions: [1, 2, 3, 4, 5, 6, 7, 8, 9],
    colors: [1, 0, 0, 0, 1, 0, 0, 0, 1],
    indices: [0, 1, 2],
    cell_m: 0.02,
  });
  expect(serializeSurface(null, null)).toBeNull();
});
