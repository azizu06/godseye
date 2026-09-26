import { expect, test } from "@playwright/test";
import { surfaceCoverage, surfaceSupport } from "../src/surfaceCoverage";
import { SurfaceEvidence } from "../src/surfaceEvidence";
import { SurfaceTiles } from "../src/surfaceTiles";
import type { CapturedSurface } from "../src/surfaceTypes";

function grid(): CapturedSurface {
  const positions = [],
    uvs = [];
  for (let y = 0; y < 4; y++)
    for (let x = 0; x < 4; x++) {
      positions.push(x * 0.02, y * 0.02, -2);
      uvs.push((x + 0.5) / 4, 1 - (y + 0.5) / 4);
    }
  return {
    id: "grid",
    sessionId: "phone",
    mapEpoch: 1,
    frameId: 1,
    capturedAt: 1,
    cameraPosition: [0, 0, 0],
    cameraForward: [0, 0, -1],
    depthWidth: 4,
    depthHeight: 4,
    positions: new Float32Array(positions),
    uvs: new Float32Array(uvs),
    colors: new Float32Array(positions.length).fill(0.5),
    indices: new Uint32Array([0, 3, 12]),
  };
}

test("dense native support promotes a checked plane immediately; sparse triangles still need revisits", () => {
  const plane = grid();
  expect(surfaceSupport(plane)[0]).toBe(10);
  expect(new SurfaceEvidence().confirm(plane, surfaceSupport(plane))).toEqual(
    plane.indices,
  );
  plane.indices = new Uint32Array([0, 1, 4]);
  expect(surfaceSupport(plane)[0]).toBe(3);
  expect(
    new SurfaceEvidence().confirm(plane, surfaceSupport(plane)),
  ).toHaveLength(0);
});

test("point retirement respects triangle boundaries, foreground details and rejected surface capacity", () => {
  const plane = grid();
  plane.positions[5 * 3 + 2] += 0.015; // painting/object above the plane
  const covered = surfaceCoverage(plane, plane.indices);
  expect(covered[5]).toBe(0);
  expect(covered[15]).toBe(0); // outside the triangle: leave the opening visible
  expect(covered[6]).toBe(1); // interior coplanar sample, not a triangle corner
  expect(covered.reduce((sum, v) => sum + v, 0)).toBe(9);
  const full = new SurfaceTiles(0, 0);
  const retained: number[] = [];
  full.add(plane, (a, b, c) => retained.push(a, b, c));
  expect(full.capacity).toBe(true);
  expect(
    surfaceCoverage(plane, new Uint32Array(retained)).every((v) => v === 0),
  ).toBe(true);
});
