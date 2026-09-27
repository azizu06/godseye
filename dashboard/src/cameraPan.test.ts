import { describe, expect, it } from "vitest";
import { PAN_REFERENCE_DISTANCE_M, panSpeedForDistance } from "./cameraPan";

describe("panSpeedForDistance", () => {
  it("keeps the stock speed at the reference distance", () => {
    expect(panSpeedForDistance(PAN_REFERENCE_DISTANCE_M)).toBe(1);
  });

  it("moves the same world distance per drag across zoom levels", () => {
    // Stock pan world distance is proportional to distance * panSpeed.
    for (const distance of [3, 6, 24])
      expect(distance * panSpeedForDistance(distance)).toBeCloseTo(
        PAN_REFERENCE_DISTANCE_M,
      );
  });

  it("clamps extreme zoom and ignores invalid distances", () => {
    expect(panSpeedForDistance(0.1)).toBe(6);
    expect(panSpeedForDistance(1000)).toBe(0.3);
    expect(panSpeedForDistance(0)).toBe(1);
    expect(panSpeedForDistance(Number.NaN)).toBe(1);
  });
});
