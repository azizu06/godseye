import { expect, it } from "vitest";
import { Simulator, FURNITURE } from "../tests/fixtures/simulator";
import {
  makeRoomSurfaces,
  SurfaceDiscovery,
} from "../tests/fixtures/simulatorSurfaces";
import type { SurfacePatch } from "./surfaceTypes";

const vertices = (surface: SurfacePatch) =>
  [...surface.indices].map(
    (index) =>
      [...surface.positions.slice(index * 3, index * 3 + 3)] as [
        number,
        number,
        number,
      ],
  );
const scan = (sim: Simulator, ticks = 50) => {
  for (let i = 0; i < ticks; i++) sim.tick(0.1);
};

it("starts with no surface and progressively reveals only in-range, in-view triangles", () => {
  const sim = new Simulator();
  expect(sim.getSurface()).toBeNull();
  sim.tick(0.1);
  const first = sim.getSurface()!;
  expect(first.indices.length).toBeGreaterThan(0);
  const firstIndices = [...first.indices];
  scan(sim);
  const surface = sim.getSurface()!;
  expect(surface.indices.length).toBeGreaterThanOrEqual(first.indices.length);
  expect([...first.indices]).toEqual(firstIndices);
  expect(surface.positions).toBe(first.positions);
  expect(surface.colors).toBe(first.colors);
  expect(sim.getSurface()).toBe(surface);
  for (const p of vertices(surface)) {
    const dx = p[0] - sim.position[0],
      dy = p[1] - sim.position[1],
      dz = p[2] - sim.position[2];
    expect(Math.hypot(dx, dy, dz)).toBeLessThanOrEqual(5.000001);
    expect(
      (dx * Math.sin(sim.yaw) + dz * Math.cos(sim.yaw)) / Math.hypot(dx, dz),
    ).toBeGreaterThanOrEqual(Math.cos(Math.PI / 6) - 1e-7);
  }
});

it("retains discovered triangles while looking away, pauses on tracking loss, and resets", () => {
  const sim = new Simulator();
  scan(sim);
  const first = sim.getSurface()!;
  sim.yaw += Math.PI;
  scan(sim);
  const next = sim.getSurface()!;
  expect(next.indices.length).toBeGreaterThan(first.indices.length);
  expect([...next.indices.slice(0, first.indices.length)]).toEqual([
    ...first.indices,
  ]);
  sim.setTracking(false);
  sim.yaw += Math.PI / 2;
  scan(sim);
  expect(sim.getSurface()).toBe(next);
  sim.command("/session");
  expect(sim.getSurface()).toBeNull();
  expect([...first.indices]).toEqual([
    ...next.indices.slice(0, first.indices.length),
  ]);
});

it("reveals the sofa front but keeps surfaces behind it occluded", () => {
  const sim = new Simulator();
  sim.position = [-2.7, 0.16, 0];
  sim.yaw = Math.PI;
  scan(sim, 100);
  const seen = vertices(sim.getSurface()!);
  expect(
    seen.some(
      ([x, y, z]) =>
        Math.abs(z + 1.45) < 1e-5 &&
        y > 0.1 &&
        y < 0.7 &&
        Math.abs(x + 2.7) < 0.4,
    ),
  ).toBe(true);
  expect(
    seen.some(([x, y, z]) => y === 0 && z < -2.5 && Math.abs(x + 2.7) < 0.3),
  ).toBe(false);
});

it("builds bounded dense color geometry for all six furniture faces and room walls", () => {
  const geometry = makeRoomSurfaces(FURNITURE);
  expect(geometry.positions.length).toBe(geometry.colors!.length);
  expect(geometry.indices.length / 3).toBeLessThan(40000);
  const box = FURNITURE[0];
  for (let axis = 0; axis < 3; axis++)
    for (const sign of [-1, 1]) {
      const plane = box.position[axis] + (sign * box.size[axis]) / 2;
      const triangles = Array.from(
        { length: geometry.indices.length / 3 },
        (_, i) => [0, 1, 2].map((j) => geometry.indices[i * 3 + j]),
      );
      expect(
        triangles.some((triangle) =>
          triangle.every((index) => {
            const p = [...geometry.positions.slice(index * 3, index * 3 + 3)];
            return (
              Math.abs(p[axis] - plane) < 1e-5 &&
              p.every(
                (v, a) =>
                  Math.abs(v - box.position[a]) <= box.size[a] / 2 + 1e-5,
              )
            );
          }),
        ),
      ).toBe(true);
    }
  expect(
    new Set([...geometry.colors!].map((v) => v.toFixed(3))).size,
  ).toBeGreaterThan(30);
  for (const channel of geometry.colors!)
    expect(channel).toBeGreaterThanOrEqual(0);
});

it("requires the centroid and every vertex to be visible before retaining a triangle", () => {
  const geometry: SurfacePatch = {
    id: "test",
    positions: new Float32Array([0, 0, 0, 3, 0, 0, 0, 0, 3]),
    indices: new Uint32Array([0, 1, 2]),
  };
  const discovery = new SurfaceDiscovery(geometry);
  discovery.scan(([x, , z]) => x !== 1 || z !== 1);
  expect(discovery.getSurface()).toBeNull();
  discovery.scan(([x]) => x !== 3);
  expect(discovery.getSurface()).toBeNull();
  discovery.scan(() => true);
  expect([...discovery.getSurface()!.indices]).toEqual([0, 1, 2]);
});

it("bounds a discovery step instead of publishing the entire hidden room", () => {
  const room = makeRoomSurfaces(FURNITURE);
  const geometry = {
    ...room,
    indices: new Uint32Array([
      ...room.indices,
      ...room.indices,
      ...room.indices,
    ]),
  };
  const discovery = new SurfaceDiscovery(geometry);
  let checks = 0;
  discovery.scan(() => {
    checks++;
    return true;
  });
  expect(checks).toBeLessThanOrEqual(3600 * 7);
  expect(discovery.getSurface()!.indices.length).toBe(3600 * 3);
  const first = discovery.getSurface()!;
  expect(first.indices.length).toBeLessThan(geometry.indices.length);
  for (let i = 0; i < 10; i++) discovery.scan(() => true);
  expect(discovery.getSurface()!.indices).toEqual(geometry.indices);
  expect(first.indices.length).toBe(3600 * 3);
  discovery.reset();
  expect(discovery.getSurface()).toBeNull();
  discovery.scan(() => true);
  expect(discovery.getSurface()!.indices).toEqual(first.indices);
});

const surfaceArea = (surface: SurfacePatch) => {
  let area = 0;
  for (let i = 0; i < surface.indices.length; i += 3) {
    const p = [0, 1, 2].map((j) => [
      ...surface.positions.slice(
        surface.indices[i + j] * 3,
        surface.indices[i + j] * 3 + 3,
      ),
    ]);
    const a = p[1].map((v, axis) => v - p[0][axis]);
    const b = p[2].map((v, axis) => v - p[0][axis]);
    area +=
      Math.hypot(
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
      ) / 2;
  }
  return area;
};

it("covers the stationary visible scene with coarse patches within two ticks", () => {
  const sim = new Simulator();
  scan(sim, 2);
  const early = sim.getSurface()!;
  const earlyArea = surfaceArea(early);
  expect(earlyArea).toBeGreaterThan(13);
  expect(earlyArea / (early.indices.length / 3)).toBeGreaterThan(0.025);
  scan(sim, 100);
  expect(earlyArea / surfaceArea(sim.getSurface()!)).toBeGreaterThan(0.95);
});

it("does not bridge an occluder crossing a coarse triangle edge", () => {
  const discovery = new SurfaceDiscovery({
    id: "edge-shadow",
    positions: new Float32Array([0, 0, 0, 0.3, 0, 0, 0, 0, 0.3]),
    indices: new Uint32Array([0, 1, 2]),
  });
  discovery.scan(([x, , z]) => Math.abs(x - 0.15) > 1e-6 || z > 0.01);
  expect(discovery.getSurface()).toBeNull();
});

it("keeps the room doorway open at coarse resolution", () => {
  const geometry = makeRoomSurfaces(FURNITURE);
  let wallTriangles = 0;
  for (let i = 0; i < geometry.indices.length; i += 3) {
    const points = [0, 1, 2].map((j) => [
      ...geometry.positions.slice(
        geometry.indices[i + j] * 3,
        geometry.indices[i + j] * 3 + 3,
      ),
    ]);
    if (!points.every((p) => Math.abs(p[2] + 3) < 1e-6)) continue;
    wallTriangles++;
    const x = points.reduce((sum, p) => sum + p[0], 0) / 3,
      y = points.reduce((sum, p) => sum + p[1], 0) / 3;
    expect(Math.abs(x) < 0.6 && y < 1.8).toBe(false);
  }
  expect(wallTriangles).toBeGreaterThan(10);
});
