import { expect, it } from "vitest";
import { DepthContradiction, type DepthObservation } from "./depthRetirement";
const observation = (capturedAt: number, depth = 3): DepthObservation => ({
  sessionId: "room",
  mapEpoch: 1,
  capturedAt,
  width: 20,
  height: 16,
  depth: new Float32Array(320).fill(depth),
  confidence: new Uint8Array(320).fill(2),
  projection: {
    transform: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    intrinsics: [40, 0, 0, 0, 40, 0, 40, 32, 1],
    imageWidth: 80,
    imageHeight: 64,
  },
});
const evidence = () => [observation(2), observation(3)];
it("requires full high-confidence free-space support in both newer frames", () => {
  const pair = evidence();
  expect(new DepthContradiction(pair).point(0, 0, -1)).toBe(true);
  expect(new DepthContradiction(pair).point(0, 0, -3)).toBe(false);
  expect(new DepthContradiction(pair).point(0, 0, -4)).toBe(false);
  expect(new DepthContradiction(pair).point(0, 0, 1)).toBe(false);
  expect(new DepthContradiction(pair).point(10, 0, -1)).toBe(false);
  for (const bad of [0, 1, 255]) {
    pair[0].confidence[8 * 20 + 10] = bad;
    expect(new DepthContradiction(pair).point(0, 0, -1)).toBe(false);
  }
  pair[0].confidence.fill(2);
  for (const bad of [NaN, 0, 8, 0.5, 1.1]) {
    pair[0].depth[8 * 20 + 10] = bad;
    expect(new DepthContradiction(pair).point(0, 0, -1)).toBe(false);
  }
});
it("preserves a coarse face with an occluded or invalid interior despite clear vertices", () => {
  const pair = evidence(),
    a = [-0.6, -0.5, -1],
    b = [0.6, -0.5, -1],
    c = [0, 0.5, -1];
  expect(new DepthContradiction(pair).triangle(a, b, c)).toBe(true);
  pair[1].depth[8 * 20 + 10] = 0.7;
  expect(new DepthContradiction(pair).triangle(a, b, c)).toBe(false);
  pair[1].depth[8 * 20 + 10] = 3;
  pair[1].confidence[8 * 20 + 10] = 0;
  expect(new DepthContradiction(pair).triangle(a, b, c)).toBe(false);
});
it("uses same-frame optical depth and off-center calibration under camera rotation/translation", () => {
  const pair = evidence();
  for (const frame of pair) {
    frame.projection.transform = [
      0, 0, -1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 2, 1, 3, 1,
    ];
    frame.projection.intrinsics = [50, 0, 0, 0, 44, 0, 36, 28, 1];
  }
  // Local camera (0,0,-1) transforms to world(1,1,3).
  expect(new DepthContradiction(pair).point(1, 1, 3)).toBe(true);
  // An unchanged world wall at optical depth3 remains despite nonzero worldZ.
  expect(new DepthContradiction(pair).point(-1, 1, 3)).toBe(false);
  expect(new DepthContradiction(pair).point(-2, 1, 3)).toBe(false);
});
it("rejects duplicate timestamps, scope mixes and malformed projection instead of carving", () => {
  const pair = evidence();
  expect(() => new DepthContradiction([pair[0], pair[0]])).toThrow();
  pair[1].mapEpoch = 2;
  expect(() => new DepthContradiction(pair)).toThrow();
  pair[1].mapEpoch = 1;
  pair[0].projection.transform[0] = 2;
  expect(() => new DepthContradiction(pair)).toThrow();
  pair[0].projection.transform[0] = 1;
  pair[0].projection.intrinsics[0] = NaN;
  expect(() => new DepthContradiction(pair)).toThrow();
});
