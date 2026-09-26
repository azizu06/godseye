import { expect, test } from "@playwright/test";
import { SurfaceEvidence } from "../src/surfaceEvidence";
import type { CapturedSurface } from "../src/surfaceTypes";

function patch(frameId: number, z = -2): CapturedSurface {
  return {
    id: String(frameId),
    sessionId: "phone",
    mapEpoch: 1,
    frameId,
    capturedAt: frameId / 5,
    positions: new Float32Array([0, 0, z, 0.02, 0, z, 0, 0.02, z]),
    indices: new Uint32Array([0, 1, 2]),
    cameraPosition: [0, 0, 0],
    cameraForward: [0, 0, -1],
  };
}

test("three distinct, consistent observations promote measured triangles without changing coordinates", () => {
  const evidence = new SurfaceEvidence();
  const first = patch(1);
  first.indices = new Uint32Array(Array(30).fill([0, 1, 2]).flat());
  expect(evidence.confirm(first)).toHaveLength(0);
  expect(evidence.confirm(first)).toHaveLength(0);
  expect(evidence.confirm(patch(2))).toHaveLength(0);
  const third = patch(3, -2.003);
  const measured = third.positions.slice();
  expect(evidence.confirm(third)).toEqual(third.indices);
  expect(third.positions).toEqual(measured);
  // Cached IDs with a different timestamp also cannot count again.
  expect(evidence.confirm({ ...third, capturedAt: 0.9 })).toHaveLength(0);
});

test("unstable depth, normals, new regions and new maps remain points", () => {
  const evidence = new SurfaceEvidence();
  evidence.confirm(patch(1));
  expect(evidence.confirm(patch(2, -2.01))).toHaveLength(0);
  expect(evidence.confirm(patch(3))).toHaveLength(0);
  const tilted = patch(4);
  tilted.positions[8] += 0.01;
  expect(evidence.confirm(tilted)).toHaveLength(0);
  expect(evidence.confirm(patch(5))).toHaveLength(0);
  expect(evidence.confirm(patch(6))).toHaveLength(0);
  expect(evidence.confirm(patch(7))).toHaveLength(3);
  const distant = patch(8);
  distant.positions = distant.positions.map((v, i) =>
    i % 3 === 0 ? v + 1 : v,
  );
  expect(evidence.confirm(distant)).toHaveLength(0);
  expect(evidence.confirm({ ...patch(1), mapEpoch: 2 })).toHaveLength(0);
  expect(evidence.size).toBe(1);
});

test("old and evicted evidence cannot promote a newly revisited region", () => {
  const evidence = new SurfaceEvidence(2);
  evidence.confirm(patch(1));
  evidence.confirm(patch(2));
  expect(evidence.confirm({ ...patch(3), capturedAt: 20 })).toHaveLength(0);
  for (let id = 101; id <= 102; id++) evidence.confirm(patch(id, -id));
  expect(evidence.size).toBe(2);
  expect(evidence.confirm(patch(103))).toHaveLength(0);
});
