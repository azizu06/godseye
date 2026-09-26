import { expect, it } from "vitest";
import { retainSurface, MAX_SURFACE_PATCHES } from "./surfaceStore";
import type { CapturedSurface } from "./surfaceTypes";
const key = JSON.stringify(["room", 1]);
function frame(t: number, x = 0): CapturedSurface {
  return {
    id: `${t}`,
    sessionId: "room",
    mapEpoch: 1,
    frameId: t,
    capturedAt: t,
    cameraPosition: [x, 0, 0],
    cameraForward: [0, 0, -1],
    positions: new Float32Array(9),
    indices: new Uint32Array([0, 1, 2]),
  };
}
it("replaces matching viewpoints, retains distinct scans and rejects older or foreign frames", () => {
  const first = retainSurface([], frame(1), key);
  const next = retainSurface(first, frame(2, 0.1), key);
  expect(next.map((p) => p.id)).toEqual(["2"]);
  const other = retainSurface(next, frame(3, 1), key);
  expect(other).toHaveLength(2);
  expect(retainSurface(other, frame(2, 3), key)).toBe(other);
  expect(retainSurface(other, { ...frame(4), mapEpoch: 2 }, key)).toBe(other);
  expect(
    retainSurface(other, { ...frame(4), indices: new Uint32Array() }, key),
  ).toBe(other);
  expect(first[0].id).toBe("1");
});
it("bounds retained geometry and image data without allowing one oversized patch", () => {
  let patches: CapturedSurface[] = [];
  for (let t = 1; t < 60; t++)
    patches = retainSurface(patches, frame(t, t), key);
  expect(patches).toHaveLength(MAX_SURFACE_PATCHES);
  expect(patches.at(-1)?.frameId).toBe(59);
  const large = { ...frame(60, 60), jpeg: new Uint8Array(49 * 1024 * 1024) };
  expect(retainSurface(patches, large, key)).toBe(patches);
});
