import { expect, it } from "vitest";
import { joystickVector, validManualCapabilities } from "./joystick";
it("uses a circular deadzone and bounded combined forward/turn vector", () => {
  expect(joystickVector(0.05, 0.05)).toEqual({ x: 0, y: 0, v: 0, w: 0 });
  const d = joystickVector(3, -3);
  expect(Math.hypot(d.x, d.y)).toBeCloseTo(1);
  expect(d.v).toBeGreaterThan(0);
  expect(d.w).toBeLessThan(0);
  expect(d.v).toBeLessThanOrEqual(0.15);
  expect(d.w).toBeLessThanOrEqual(0.5);
  expect(joystickVector(0, 1).v).toBe(-0.15);
});
it("respects physical adapter supported directions", () => {
  expect(
    joystickVector(0, 1, {
      forward: [0.05, 0.2],
      reverse: null,
      yaw: [0.05, 0.5],
      arcs: true,
    }).v,
  ).toBe(0);
  const measured = joystickVector(0.8, -0.4, {
    forward: [0.05, 0.2],
    reverse: [0.05, 0.2],
    yaw: [0.05, 0.5],
    arcs: false,
  });
  expect(measured.v).toBe(0);
  expect(measured.w).toBeLessThan(0);
  expect(
    joystickVector(0.2, -0.8, {
      forward: [0.05, 0.2],
      reverse: [0.05, 0.2],
      yaw: [0.05, 0.5],
      arcs: false,
    }).w,
  ).toBe(0);
});

it("rejects malformed capabilities instead of producing unsafe or NaN motion", () => {
  expect(
    validManualCapabilities({
      forward: [0, 0.2],
      reverse: null,
      yaw: [0, 0.5],
      arcs: true,
    }),
  ).toBe(true);
  for (const forward of [[0.2, 0.1], [0, 2], [0, NaN], true, undefined])
    expect(
      validManualCapabilities({
        forward,
        reverse: null,
        yaw: [0, 0.5],
        arcs: true,
      }),
    ).toBe(false);
});

it("accepts explicit unavailable capability metadata", () => {
  expect(validManualCapabilities(null)).toBe(true);
});
