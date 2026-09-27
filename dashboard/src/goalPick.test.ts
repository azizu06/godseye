import { describe, expect, it } from "vitest";
import { pickApproachTarget } from "./goalPick";

// Camera 2 m up at the origin, looking down-forward along +Z at 45 degrees.
const origin = [0, 2, 0] as const;
const down = [0, -1, 1] as const;

describe("pickApproachTarget", () => {
  it("lands on the floor when no measured point is on the ray", () => {
    const points = new Float32Array([3, 0.5, 0.2]);
    const [x, z] = pickApproachTarget(origin, down, points, 1, 0)!;
    expect(x).toBeCloseTo(0);
    expect(z).toBeCloseTo(2);
  });

  it("picks the front face of a box instead of the floor behind it", () => {
    // A box face at z=1.5 whose points straddle the ray (the ray is at y=0.5 there).
    const points = new Float32Array([
      0, 0.5, 1.5, 0.01, 0.52, 1.49, 0, 0.1, 1.9,
    ]);
    const [x, z] = pickApproachTarget(origin, down, points, 3, 0)!;
    expect(x).toBeCloseTo(0, 1);
    expect(z).toBeCloseTo(1.5, 1);
  });

  it("chooses the nearest of several points along the ray", () => {
    const points = new Float32Array([0, 0.4, 1.6, 0, 1, 1]);
    const [, z] = pickApproachTarget(origin, down, points, 2, 0)!;
    expect(z).toBeCloseTo(1);
  });

  it("ignores points below the floor hit and beyond count", () => {
    const points = new Float32Array([0, -0.5, 2.5, 0, 1, 1]);
    const [, z] = pickApproachTarget(origin, down, points, 1, 0)!;
    expect(z).toBeCloseTo(2);
  });

  it("reaches a wall above the horizon with no floor hit", () => {
    const points = new Float32Array([0, 2.5, 4]);
    const [x, z] = pickApproachTarget(origin, [0, 0.125, 1], points, 1, 0)!;
    expect(x).toBeCloseTo(0);
    expect(z).toBeCloseTo(4);
  });

  it("returns null when the ray meets neither points nor floor", () => {
    expect(
      pickApproachTarget(origin, [0, 1, 0], new Float32Array(), 0, 0),
    ).toBe(null);
    expect(pickApproachTarget(origin, down, new Float32Array(), 0, null)).toBe(
      null,
    );
  });
});
