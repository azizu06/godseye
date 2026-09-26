import { expect, it } from "vitest";
import { LIDAR_RANGE_M, scopeArc, scopeTriangles } from "./sensorProfile";
it("draws the Apple-documented five-meter range without extending corner rays beyond it", () => {
  const arc = scopeArc();
  expect(LIDAR_RANGE_M).toBe(5);
  expect(arc.length).toBeGreaterThan(10);
  for (const p of arc) expect(Math.hypot(p[0], p[2])).toBeCloseTo(5, 6);
  expect(arc[Math.floor(arc.length / 2)][2]).toBeCloseTo(5);
  const triangles = scopeTriangles();
  for (let i = 0; i < triangles.length; i += 3)
    expect(Math.hypot(triangles[i], triangles[i + 2])).toBeLessThanOrEqual(
      5.000001,
    );
});
