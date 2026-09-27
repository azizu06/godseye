import { expect, it, vi } from "vitest";
import { PersistentSurfaceMap } from "./persistentSurfaceMap";
import { DepthContradiction, type DepthObservation } from "./depthRetirement";
import { SurfaceBuffer } from "./surfaceBuffer";
import { serializeSurface } from "./surfaceColor";
const patch = (z: number, color: number[]) => ({
  id: String(z),
  positions: new Float32Array([-0.2, -0.2, z, 0.2, -0.2, z, 0, 0.2, z]),
  indices: new Uint32Array([0, 1, 2]),
  colors: new Float32Array([...color, ...color, ...color]),
});
const observation = (t: number): DepthObservation => ({
  sessionId: "room",
  mapEpoch: 1,
  capturedAt: t,
  width: 20,
  height: 16,
  depth: new Float32Array(320).fill(3),
  confidence: new Uint8Array(320).fill(2),
  projection: {
    transform: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    intrinsics: [40, 0, 0, 0, 40, 0, 40, 32, 1],
    imageWidth: 80,
    imageHeight: 64,
  },
});
const proof = () => new DepthContradiction([observation(2), observation(3)]);
it("retires indexed red foreground, exports no orphan vertices, and admits blue reoccupation under budget", () => {
  const map = new PersistentSurfaceMap({ maxVertices: 6, maxTriangles: 2 }),
    buffer = new SurfaceBuffer();
  map.add(patch(-3, [0.5, 0.5, 0.5]));
  map.add(patch(-1, [1, 0, 0]));
  buffer.apply(map.takeDelta());
  expect(map.retire(proof())).toBe(1);
  const exported = serializeSurface(buffer.apply(map.takeDelta()), 0)!;
  expect(exported.positions).toHaveLength(9);
  expect(exported.positions.filter((_, i) => i % 3 === 2)).toEqual([
    -3, -3, -3,
  ]);
  map.add(patch(-1, [0, 0, 1])); // Reclaims orphan slots before considering coarsening/capacity.
  expect(map.triangleCount).toBe(2);
  const fresh = serializeSurface(buffer.apply(map.takeDelta()), 0)!;
  const front = fresh.positions
    .map((_, i) => i)
    .filter((i) => i % 3 === 2 && fresh.positions[i] === -1);
  expect(front).toHaveLength(3);
  for (const z of front)
    expect(fresh.colors.slice(z - 2, z + 1)).toEqual([0, 0, 1]);
});
it("rolls back a failed retirement transaction including dedup keys and future removal", () => {
  const map = new PersistentSurfaceMap();
  map.add(patch(-3, [0.5, 0.5, 0.5]));
  map.add(patch(-1, [1, 0, 0]));
  const before = map.snapshot();
  const undo = map.retirementCheckpoint();
  expect(map.retire(proof())).toBe(1);
  undo();
  expect(map.snapshot()).toBe(before);
  map.add(patch(-1, [1, 0, 0]));
  expect(map.triangleCount).toBe(2);
  expect(map.retire(proof())).toBe(1);
  expect(map.triangleCount).toBe(1);
});
it("publishes an empty renderer update when all observed faces are disproved", () => {
  const map = new PersistentSurfaceMap(),
    buffer = new SurfaceBuffer();
  map.add(patch(-1, [1, 0, 0]));
  buffer.apply(map.takeDelta());
  expect(map.retire(proof())).toBe(1);
  expect(buffer.apply(map.takeDelta())).toBeNull();
  expect(map.snapshot()).toBeNull();
});

it("continues through the recent face tail when each capture exhausts its soft budget", () => {
  const map = new PersistentSurfaceMap();
  for (let i = 0; i < 512; i++) {
    const face = patch(-1, [1, 0, 0]);
    for (let j = 0; j < face.positions.length; j += 3) face.positions[j] += i;
    map.add(face);
  }
  let time = 0;
  const clock = vi
    .spyOn(performance, "now")
    .mockImplementation(() => (time += 20));
  try {
    const evidence = {
      triangle: (a: ArrayLike<number>) => a[0] > 200 && a[0] < 260,
    } as unknown as DepthContradiction;
    let removed = 0;
    for (let frame = 0; frame < 8; frame++) removed += map.retire(evidence);
    expect(removed).toBeGreaterThan(0);
  } finally {
    clock.mockRestore();
  }
});
