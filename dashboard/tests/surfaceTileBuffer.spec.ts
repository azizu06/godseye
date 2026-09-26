import { expect, test } from "@playwright/test";
import { BufferAttribute, Vector3 } from "three";
import { SurfaceTiles } from "../src/surfaceTiles";
import { SurfaceTileBuffer } from "../src/surfaceTileBuffer";
import {
  createTileGeometry,
  syncTileGeometry,
} from "../src/surfaceTileGeometry";
import type { SurfacePatch } from "../src/surfaceTypes";

function grid(size: number): SurfacePatch {
  const p: number[] = [],
    indices: number[] = [];
  for (let y = 0; y < size; y++)
    for (let x = 0; x < size; x++) {
      p.push(0.01 + x * 0.004, 0.01 + y * 0.004, 0.1);
      if (x < size - 1 && y < size - 1) {
        const a = y * size + x;
        indices.push(a, a + size, a + 1, a + 1, a + size, a + size + 1);
      }
    }
  return {
    id: "grid",
    positions: new Float32Array(p),
    colors: new Float32Array(p.length).fill(0.5),
    indices: new Uint32Array(indices),
  };
}
function recolor(patch: SurfacePatch, value: number): SurfacePatch {
  return {
    ...patch,
    colors: new Float32Array(patch.positions.length).fill(value),
    indices: patch.indices.slice(0, 3),
  };
}
function uploaded(geometry: ReturnType<typeof createTileGeometry>) {
  for (const attribute of [
    geometry.attributes.position,
    geometry.attributes.color,
    geometry.index,
  ])
    (attribute as BufferAttribute).clearUpdateRanges();
}

test("a small observation updates only its vertices and never resends retained triangle indices", () => {
  const map = new SurfaceTiles(),
    input = grid(128);
  const first = map.add(input)[0],
    buffer = new SurfaceTileBuffer(first.id);
  buffer.apply(first);
  const geometry = createTileGeometry(buffer);
  syncTileGeometry(geometry, buffer);
  uploaded(geometry);
  const position = geometry.attributes.position,
    topology = geometry.index!;
  const indexVersion = topology.version;
  const before = buffer.positions.slice();
  const update = map.add(recolor(input, 0.9))[0];
  expect(update.positions.length).toBe(9);
  expect(update.colors.length).toBe(9);
  expect(update.indices.length).toBe(0);
  expect(update.indexStart).toBe(first.indexCount);
  buffer.apply(update);
  syncTileGeometry(geometry, buffer);
  expect(buffer.positions).toEqual(before);
  expect(geometry.attributes.position).toBe(position);
  expect(geometry.index).toBe(topology);
  expect(topology.version).toBe(indexVersion);
  expect((position as BufferAttribute).updateRanges).toEqual([
    { start: 0, count: 9 },
  ]);
  expect(geometry.drawRange.count).toBe(input.indices.length);
  for (let i = 0; i < 9; i++) expect(buffer.colors[i]).toBeCloseTo(0.9);
  expect(buffer.colors[9]).toBe(0.5);
  expect(buffer.indices.subarray(0, buffer.indexCount)).toEqual(first.indices);
  geometry.dispose();
});

test("appended geometry survives amortized growth without losing previous measurements", () => {
  const map = new SurfaceTiles(),
    input = grid(16);
  const firstInput = { ...input, indices: input.indices.slice(0, 3) };
  const first = map.add(firstInput)[0],
    buffer = new SurfaceTileBuffer(first.id);
  buffer.apply(first);
  const positions = buffer.positions,
    firstPositions = positions.slice(0, 9);
  const second = map.add(input)[0];
  expect(second.indexStart).toBe(3);
  expect(second.indices.length).toBe(input.indices.length - 3);
  buffer.apply(second);
  expect(buffer.positions).not.toBe(positions);
  expect(buffer.positions.slice(0, 9)).toEqual(firstPositions);
  expect(buffer.vertexCount).toBe(256);
  expect(buffer.indexCount).toBe(input.indices.length);
  expect(buffer.indices.subarray(0, 3)).toEqual(first.indices);
  expect(buffer.indices.subarray(3, buffer.indexCount)).toEqual(second.indices);
  expect(buffer.positions.length / 3).toBeLessThanOrEqual(
    buffer.vertexCount * 2,
  );
  const geometry = createTileGeometry(buffer);
  syncTileGeometry(geometry, buffer);
  expect(geometry.boundingSphere!.center.x).toBeGreaterThan(0);
  for (let i = 0; i < buffer.vertexCount * 3; i += 3)
    expect(
      geometry.boundingSphere!.containsPoint(
        new Vector3(...buffer.positions.slice(i, i + 3)),
      ),
    ).toBe(true);
  geometry.dispose();
});

test("separated refinements leave all intervening measurements intact", () => {
  const map = new SurfaceTiles(),
    input = grid(64);
  const first = map.add(input)[0],
    buffer = new SurfaceTileBuffer(first.id);
  buffer.apply(first);
  const expected = buffer.colors.slice();
  const touched = [...first.indices.slice(0, 3), ...first.indices.slice(-3)];
  for (const index of touched)
    expected.fill(Math.fround(0.9), index * 3, index * 3 + 3);
  const update = map.add({
    ...input,
    colors: new Float32Array(input.positions.length).fill(0.9),
    indices: new Uint32Array([
      ...input.indices.slice(0, 3),
      ...input.indices.slice(-3),
    ]),
  })[0];
  expect(update.spans.length).toBeGreaterThanOrEqual(4);
  expect(update.positions.length).toBeLessThan(100);
  buffer.apply(update);
  expect(buffer.colors).toEqual(expected);
  expect(buffer.indices.subarray(0, buffer.indexCount)).toEqual(first.indices);
});

test("a new observed face using existing vertices uploads only appended topology", () => {
  const map = new SurfaceTiles(),
    input = grid(2);
  const first = map.add(input)[0],
    buffer = new SurfaceTileBuffer(first.id);
  buffer.apply(first);
  const geometry = createTileGeometry(buffer);
  syncTileGeometry(geometry, buffer);
  uploaded(geometry);
  const update = map.add({ ...input, indices: new Uint32Array([0, 1, 3]) })[0];
  expect(update.positions.length).toBe(0);
  expect(update.spans.length).toBe(0);
  expect(update.indices.length).toBe(3);
  buffer.apply(update);
  syncTileGeometry(geometry, buffer);
  expect(
    (geometry.attributes.position as BufferAttribute).updateRanges,
  ).toEqual([]);
  expect(geometry.index!.updateRanges).toEqual([{ start: 6, count: 3 }]);
  expect(geometry.drawRange.count).toBe(9);
  geometry.dispose();
});

test("paused rendering coalesces worker and GPU backlogs while preserving the latest colors", () => {
  const map = new SurfaceTiles(),
    input = grid(8);
  const first = map.add(input)[0],
    buffer = new SurfaceTileBuffer(first.id);
  buffer.apply(first);
  const geometry = createTileGeometry(buffer);
  syncTileGeometry(geometry, buffer);
  uploaded(geometry);
  for (let i = 0; i < 1000; i++) {
    buffer.apply(map.add(recolor(input, 0.6 + i / 10000))[0]);
    if (i % 10 === 0) syncTileGeometry(geometry, buffer);
  }
  syncTileGeometry(geometry, buffer);
  expect((geometry.attributes.color as BufferAttribute).updateRanges).toEqual([
    { start: 0, count: 9 },
  ]);
  expect(geometry.index!.updateRanges).toEqual([]);
  expect(buffer.colors[0]).toBeCloseTo(0.6999);
  // P mode unmounts surfaces. Recreating the geometry needs all retained data,
  // even when the last renderer already consumed the dirty ranges.
  geometry.dispose();
  const remounted = createTileGeometry(buffer);
  syncTileGeometry(remounted, buffer);
  expect(remounted.drawRange.count).toBe(input.indices.length);
  expect(remounted.getAttribute("color").getX(0)).toBeCloseTo(0.6999);
  remounted.dispose();
});

test("out-of-order, incomplete, and oversized deltas cannot partially corrupt a tile", () => {
  const map = new SurfaceTiles(),
    input = grid(8);
  const first = map.add(input)[0],
    buffer = new SurfaceTileBuffer(first.id);
  buffer.apply(first);
  const update = map.add(recolor(input, 0.9))[0];
  const before = buffer.colors.slice();
  for (const invalid of [
    { ...update, revision: update.revision + 1 },
    { ...update, id: "other-tile" },
    { ...update, vertexCount: 500_001 },
    { ...update, vertexCount: update.vertexCount + 1 },
    { ...update, indexStart: update.indexStart - 3 },
    { ...update, spans: new Uint32Array([0, 6]) },
  ]) {
    expect(() => buffer.apply(invalid)).toThrow();
    expect(buffer.colors).toEqual(before);
    expect(buffer.revision).toBe(1);
  }
  buffer.apply(update);
  expect(buffer.revision).toBe(2);
});
