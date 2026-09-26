import { expect, it } from "vitest";
import { capture } from "../tests/captureFixture";
import { decodeCaptureSurface } from "./captureSurface";
import { PersistentSurfaceMap } from "./persistentSurfaceMap";
import { SurfaceBuffer } from "./surfaceBuffer";
import { retainedCoverage } from "./retainedCoverage";
import { PointCloudStore } from "./pointCloud";
import type { SurfacePatch } from "./surfaceTypes";

const triangle = (x: number): SurfacePatch => ({
  id: String(x),
  positions: new Float32Array([x, 0, 0, x + 1, 0, 0, x, 1, 0]),
  colors: new Float32Array(9).fill(0.5),
  indices: new Uint32Array([0, 1, 2]),
});
it("streams only appended retained geometry and keeps renderer allocations stable", () => {
  const map = new PersistentSurfaceMap(),
    renderer = new SurfaceBuffer();
  map.add(triangle(0));
  const first = renderer.apply(map.takeDelta())!;
  map.add(triangle(2));
  const delta = map.takeDelta();
  expect(delta.reset).toBe(false);
  expect([
    delta.vertexStart,
    delta.indexStart,
    delta.positions.length,
    delta.indices.length,
  ]).toEqual([3, 3, 9, 3]);
  const second = renderer.apply(delta)!;
  expect(second.update!.positions).toBe(first.update!.positions);
  expect(second.update!.indices).toBe(first.update!.indices);
  expect(second.positions).toEqual(map.snapshot()!.positions);
  expect(second.indices).toEqual(map.snapshot()!.indices);
  map.add(triangle(2));
  const unchanged = map.takeDelta();
  expect(unchanged.positions.length + unchanged.indices.length).toBe(0);
  expect(renderer.apply(unchanged)).toBe(second);
});
it("capacity failures leave retained geometry and incremental cursors intact", () => {
  const map = new PersistentSurfaceMap({ maxTriangles: 1, maxVertices: 3 });
  const renderer = new SurfaceBuffer();
  map.add(triangle(0));
  const first = renderer.apply(map.takeDelta());
  expect(() => map.add(triangle(10))).toThrow(/capacity/);
  expect(renderer.apply(map.takeDelta())).toBe(first);
  expect(map.triangleCount).toBe(1);
});
it("replacement topology leaves an already published surface intact", () => {
  const map = new PersistentSurfaceMap(),
    renderer = new SurfaceBuffer();
  map.add(triangle(0));
  const first = renderer.apply(map.takeDelta())!;
  const original = first.positions.slice();
  const replacement = triangle(10);
  const next = renderer.apply({
    revision: first.update!.revision + 1,
    reset: true,
    vertexStart: 0,
    indexStart: 0,
    vertexCount: 3,
    indexCount: 3,
    positions: replacement.positions,
    colors: replacement.colors!,
    indices: replacement.indices,
  })!;
  expect(first.positions).toEqual(original);
  expect(next.positions).toEqual(replacement.positions);
  expect(next.update!.positions).not.toBe(first.update!.positions);
  expect(next.update!.indices).not.toBe(first.update!.indices);
});
it("compressed surfaces retire coplanar points but preserve a measured hole and foreground", () => {
  const packet = capture();
  const surface = decodeCaptureSurface(
    packet.buffer.slice(
      packet.byteOffset,
      packet.byteOffset + packet.byteLength,
    ) as ArrayBuffer,
    true,
  );
  const hole = 7 * 20 + 10;
  const indices: number[] = [];
  for (let i = 0; i < surface.indices.length; i += 3) {
    const face = [...surface.indices.subarray(i, i + 3)];
    if (!face.includes(hole)) indices.push(...face);
  }
  const map = new PersistentSurfaceMap();
  const accepted = map.add({
    ...surface,
    indices: new Uint32Array(indices),
    colors: new Float32Array(surface.positions.length).fill(0.5),
  });
  expect(accepted.retained!.positions.length).toBeLessThan(
    surface.positions.length,
  );
  const mask = retainedCoverage(surface, accepted.retained!);
  expect(mask[hole]).toBe(0);
  expect(mask[0]).toBe(1);
  expect(mask.reduce((a, b) => a + b, 0)).toBeGreaterThan(200);
  surface.positions[2] += 0.1;
  expect(retainedCoverage(surface, accepted.retained!)[0]).toBe(0);
});
it("a same-frame completed mesh updates preview coverage and coarsening restores dots", () => {
  const store = new PointCloudStore(8);
  const points = {
    sessionId: "room",
    mapEpoch: 0,
    frameId: 1,
    capturedAt: 1,
    positions: new Float32Array([0, 0, -2, 1, 0, -2]),
    colors: new Float32Array(6).fill(0.5),
  };
  expect(store.ingestCaptured(points)).toBe("accepted");
  expect(store.visibleCount).toBe(2);
  expect(
    store.ingestCaptured({ ...points, covered: new Uint8Array([1, 0]) }),
  ).toBe("accepted");
  expect(store.visibleCount).toBe(1);
  store.restoreCoverage();
  expect(store.visibleCount).toBe(2);
});
