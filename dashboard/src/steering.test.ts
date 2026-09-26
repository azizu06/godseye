import { afterEach, describe, expect, it, vi } from "vitest";
import {
  headingForDirection,
  steeringVelocity,
  DirectionalSteering,
} from "./steering";

afterEach(() => vi.useRealTimers());

describe("view-relative steering", () => {
  it("maps screen arrows to ground headings, including an orbited view", () => {
    expect(headingForDirection("down", 0)).toBeCloseTo(0);
    expect(Math.abs(headingForDirection("up", 0))).toBeCloseTo(Math.PI);
    expect(headingForDirection("right", 0)).toBeCloseTo(Math.PI / 2);
    expect(headingForDirection("left", 0)).toBeCloseTo(-Math.PI / 2);
    expect(headingForDirection("down", Math.PI / 2)).toBeCloseTo(Math.PI / 2);
    expect(headingForDirection("right", Math.PI / 2)).toBeCloseTo(Math.PI);
  });
  it("turns in place for an opposite direction instead of reversing", () => {
    const velocity = steeringVelocity(Math.PI, 0);
    expect(velocity.v_mps).toBe(0);
    expect(Math.abs(velocity.yaw_rate_rps)).toBe(0.5);
    expect(steeringVelocity(0.05, 0).v_mps).toBe(0.15);
  });
  it("takes the shortest turn across the angle wrap", () => {
    const velocity = steeringVelocity(Math.PI - 0.2, -Math.PI + 0.2);
    expect(velocity.v_mps).toBe(0);
    expect(velocity.yaw_rate_rps).toBeGreaterThan(0);
    expect(velocity.yaw_rate_rps).toBeLessThanOrEqual(0.5);
  });
  it("recomputes motion from the live pose then releases to zero", async () => {
    vi.useFakeTimers();
    let yaw = Math.PI;
    const sent: { v_mps: number; yaw_rate_rps: number }[] = [];
    const steering = new DirectionalSteering(
      async (body) => {
        sent.push(body);
      },
      () => ({ yaw, viewYaw: 0, ready: true }),
      () => {},
    );
    steering.start("down");
    await vi.advanceTimersByTimeAsync(100);
    expect(sent[0].v_mps).toBe(0);
    yaw = 0;
    await vi.advanceTimersByTimeAsync(100);
    expect(sent.at(-1)?.v_mps).toBe(0.15);
    steering.stop();
    await vi.advanceTimersByTimeAsync(1000);
    expect(sent.at(-1)).toEqual({ v_mps: 0, yaw_rate_rps: 0 });
    expect(sent.filter((x) => x.v_mps > 0)).toHaveLength(1);
  });
  it("stops when readiness fails and never queues network pulses", async () => {
    vi.useFakeTimers();
    let ready = true;
    const sent: { v_mps: number; yaw_rate_rps: number }[] = [];
    const steering = new DirectionalSteering(
      async (body) => {
        sent.push(body);
        if (body.v_mps) await new Promise(() => {});
      },
      () => ({ yaw: 0, viewYaw: 0, ready }),
      () => {},
    );
    steering.start("down");
    await vi.advanceTimersByTimeAsync(500);
    expect(sent).toHaveLength(1);
    ready = false;
    await vi.advanceTimersByTimeAsync(100);
    expect(sent.at(-1)).toEqual({ v_mps: 0, yaw_rate_rps: 0 });
  });
});
