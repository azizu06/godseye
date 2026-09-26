import { expect, it } from "vitest";
import { SurfaceKeyframes } from "./surfaceKeyframes";
import type { CapturedSurface } from "./surfaceTypes";
function view(x = 0, z = 0): CapturedSurface {
  return {
    id: `${x}:${z}`,
    sessionId: "room",
    mapEpoch: 1,
    frameId: 1,
    capturedAt: 1,
    cameraPosition: [x, 0, 0],
    cameraForward: [0, 0, -1],
    positions: new Float32Array([0, 0, z, 1, 0, z, 0, 1, z, 1, 1, z]),
    indices: new Uint32Array([0, 1, 2, 1, 3, 2]),
  };
}
it("skips redundant successful views but admits a new viewpoint", () => {
  const gate = new SurfaceKeyframes(),
    first = view();
  expect(gate.shouldIntegrate(first)).toBe(true);
  // A pending/failed fusion has not committed this reference.
  expect(gate.shouldIntegrate(first)).toBe(true);
  gate.remember(first);
  expect(gate.shouldIntegrate(view(0.02))).toBe(false);
  expect(gate.shouldIntegrate(view(0.3))).toBe(true);
  expect(gate.shouldIntegrate({ ...view(), cameraForward: [1, 0, 0] })).toBe(
    true,
  );
});
it("admits newly supported or moved surfaces from a stationary camera", () => {
  const gate = new SurfaceKeyframes();
  gate.remember({ ...view(), indices: new Uint32Array([0, 1, 2]) });
  expect(gate.shouldIntegrate(view())).toBe(true);
  gate.remember(view());
  expect(gate.shouldIntegrate(view(0, 0.3))).toBe(true);
});
it("bounds reference memory and never compares a different map identity", () => {
  const gate = new SurfaceKeyframes();
  for (let i = 0; i < 40; i++) gate.remember(view(i));
  expect(gate.size).toBeLessThanOrEqual(16);
  expect(gate.shouldIntegrate({ ...view(39), mapEpoch: 2 })).toBe(true);
  expect(new SurfaceKeyframes().shouldIntegrate(view(39))).toBe(true);
});

it("admits any changed supported sample beyond the actual five-centimeter tolerance", () => {
  const gate = new SurfaceKeyframes();
  gate.remember(view());
  const moved = view();
  moved.positions[2] = 0.051;
  expect(gate.shouldIntegrate(moved)).toBe(true);
  expect(gate.shouldIntegrate(view(0.1))).toBe(true);
});
it("admits newly observed triangle interiors even when all corner positions were already seen", () => {
  const gate = new SurfaceKeyframes();
  const corners = {
    ...view(),
    positions: new Float32Array([
      0, 0, 0, 0.02, 0, 0, 0, 0.02, 0, 1, 0, 0, 1.02, 0, 0, 1, 0.02, 0, 0, 1, 0,
      0.02, 1, 0, 0, 1.02, 0,
    ]),
    indices: new Uint32Array([0, 1, 2, 3, 4, 5, 6, 7, 8]),
  };
  gate.remember(corners);
  expect(
    gate.shouldIntegrate({ ...corners, indices: new Uint32Array([0, 3, 6]) }),
  ).toBe(true);
});
