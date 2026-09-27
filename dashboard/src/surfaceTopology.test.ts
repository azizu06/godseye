import { expect, it } from "vitest";
import { DepthContradiction, type DepthObservation } from "./depthRetirement";
import { PersistentSurfaceMap } from "./persistentSurfaceMap";
import { SurfaceBuffer, type SurfaceDelta } from "./surfaceBuffer";
import { serializeSurface } from "./surfaceColor";
import type { SurfacePatch } from "./surfaceTypes";

// Disconnected background faces at 3 m; one 1 m foreground face is disproved.
const face = (x: number, z: number, color = 0.5): SurfacePatch => ({
  id: `${x}:${z}`,
  positions: new Float32Array([
    x - 0.01,
    -0.01,
    z,
    x + 0.01,
    -0.01,
    z,
    x,
    0.01,
    z,
  ]),
  colors: new Float32Array(9).fill(color),
  indices: new Uint32Array([0, 1, 2]),
});
const observation = (t: number): DepthObservation => ({
  sessionId: "room",
  mapEpoch: 1,
  capturedAt: t,
  width: 20,
  height: 16,
  depth: new Float32Array(320).fill(3.5),
  confidence: new Uint8Array(320).fill(2),
  projection: {
    transform: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    intrinsics: [40, 0, 0, 0, 40, 0, 40, 32, 1],
    imageWidth: 80,
    imageHeight: 64,
  },
});
const proof = () => new DepthContradiction([observation(2), observation(3)]);
const bytes = (d: SurfaceDelta) =>
  d.positions.byteLength + d.colors.byteLength + d.indices.byteLength;
/** Every drawn triangle as its three positions, for exact geometry comparison. */
const drawn = (patch: SurfacePatch | null) => {
  if (!patch) return [];
  const faces: string[] = [];
  for (let i = 0; i < patch.indices.length; i += 3)
    faces.push(
      [0, 1, 2]
        .map((j) =>
          [
            ...patch.positions.subarray(
              patch.indices[i + j] * 3,
              patch.indices[i + j] * 3 + 3,
            ),
          ].join(","),
        )
        .join("|"),
    );
  return faces;
};
function scan(background: number, foregroundAt: number) {
  const map = new PersistentSurfaceMap();
  for (let i = 0; i < background; i++) {
    if (i === foregroundAt) map.add(face(0, -1, 1));
    // Outside the proof views' footprint: the retained wall is never disproved.
    map.add(face(40 + i * 0.05, -3));
  }
  return map;
}

it("removing one face republishes only the index tail, not positions or colors", () => {
  const map = scan(1000, 990),
    buffer = new SurfaceBuffer();
  const before = buffer.apply(map.takeDelta())!;
  expect(drawn(before)).toHaveLength(1001);
  expect(map.retire(proof())).toBe(1);
  const delta = map.takeDelta();
  expect(delta.reset).toBe(false);
  expect(delta.positions.length + delta.colors.length).toBe(0);
  expect(delta.indexStart).toBe(990 * 3);
  expect(delta.indices.length).toBe(10 * 3);
  const after = buffer.apply(delta)!;
  // Vertex storage is reused; the published earlier view keeps its topology.
  expect(after.update!.positions).toBe(before.update!.positions);
  expect(after.update!.colors).toBe(before.update!.colors);
  expect(drawn(before)).toHaveLength(1001);
  expect(drawn(after)).toEqual(drawn(map.snapshot()));
  expect(drawn(after).some((f) => f.includes(",-1"))).toBe(false);
  // Export compacts the orphaned foreground vertices.
  const exported = serializeSurface(after, 0)!;
  expect(exported.positions).toHaveLength(1000 * 9);
});

it("interleaves deletion with appended views and a cancelled, unpublished delta", () => {
  const map = scan(200, 5),
    buffer = new SurfaceBuffer();
  buffer.apply(map.takeDelta());
  expect(map.retire(proof())).toBe(1);
  map.add(face(60, -3, 0.2));
  const mixed = map.takeDelta();
  expect(mixed.reset).toBe(false);
  expect(mixed.positions.length).toBe(9);
  expect(mixed.indexStart).toBe(5 * 3);
  // A cancelled caller still drains this delta but never publishes its patch.
  buffer.apply(mixed);
  map.add(face(61, -3, 0.3));
  map.add(face(0, -1, 1)); // Reoccupied later: a new observation is admitted again.
  const next = buffer.apply(map.takeDelta())!;
  expect(drawn(next)).toEqual(drawn(map.snapshot()));
  expect(drawn(next).filter((f) => f.includes(",-1"))).toHaveLength(1);
});

it("a rolled back retirement publishes nothing but the ordinary append", () => {
  const map = scan(50, 3),
    buffer = new SurfaceBuffer();
  buffer.apply(map.takeDelta());
  const undo = map.retirementCheckpoint();
  expect(map.retire(proof())).toBe(1);
  undo();
  map.add(face(70, -3));
  const delta = map.takeDelta();
  expect([delta.reset, delta.indexStart, delta.indices.length]).toEqual([
    false,
    51 * 3,
    3,
  ]);
  expect(drawn(buffer.apply(delta))).toEqual(drawn(map.snapshot()));
});

it("genuine coarsening or orphan reclamation still publishes a full replacement", () => {
  const map = new PersistentSurfaceMap({ maxVertices: 9, maxTriangles: 3 }),
    buffer = new SurfaceBuffer();
  map.add(face(40, -3));
  map.add(face(0, -1, 1));
  map.add(face(41, -3));
  buffer.apply(map.takeDelta());
  expect(map.retire(proof())).toBe(1);
  // Budget pressure reclaims orphaned vertex slots: stable IDs change.
  map.add(face(42, -3));
  expect(map.triangleCount).toBe(3);
  const delta = map.takeDelta();
  expect(delta.reset).toBe(true);
  expect(drawn(buffer.apply(delta))).toEqual(drawn(map.snapshot()));
});

it.each([12_000, 120_000])(
  "one deletion from a %i-face map transfers index bytes only after the removed face",
  (faces) => {
    const map = scan(faces, faces - 100),
      buffer = new SurfaceBuffer();
    buffer.apply(map.takeDelta());
    expect(map.retire(proof())).toBe(1);
    const delta = map.takeDelta();
    expect(delta.reset).toBe(false);
    // The previous full replacement republished every vertex: ~72 bytes per face here.
    expect(bytes(delta)).toBe(100 * 12);
    expect(drawn(buffer.apply(delta))).toHaveLength(faces);
  },
);
