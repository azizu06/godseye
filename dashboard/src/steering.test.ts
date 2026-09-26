import { afterEach, describe, expect, it, vi } from "vitest";
import {
  headingForDirection,
  steeringVelocity,
  DirectionalSteering,
  type SteeringVelocity,
} from "./steering";

afterEach(() => vi.useRealTimers());

describe("rover-relative steering", () => {
  it("maps arrows relative to the rover heading with left/right in its forward frame", () => {
    expect(headingForDirection("up", 2.55)).toBeCloseTo(2.55);
    expect(Math.abs(headingForDirection("down", 0))).toBeCloseTo(Math.PI);
    expect(headingForDirection("right", 0)).toBeCloseTo(-Math.PI / 2);
    expect(headingForDirection("left", 0)).toBeCloseTo(Math.PI / 2);
    expect(headingForDirection("right", Math.PI / 2)).toBeCloseTo(0);
    expect(Math.abs(headingForDirection("left", Math.PI / 2))).toBeCloseTo(
      Math.PI,
    );
  });
  it.each([0, 2.55, -2.8, Math.PI / 3, 10 * Math.PI + 0.4])(
    "starts forward immediately at heading %s without a preliminary turn",
    async (yaw) => {
      vi.useFakeTimers();
      const sent: SteeringVelocity[] = [];
      const steering = new DirectionalSteering(
        async (body) => {
          sent.push(body);
        },
        () => ({ yaw, ready: true }),
        () => {},
      );
      steering.start("up");
      await vi.advanceTimersByTimeAsync(200);
      expect(sent).toHaveLength(3);
      for (const velocity of sent)
        expect(velocity).toEqual({ v_mps: 0.15, yaw_rate_rps: 0 });
      steering.stop();
    },
  );
  it("turns in place for an opposite direction instead of reversing", () => {
    const velocity = steeringVelocity(2.55, headingForDirection("down", 2.55));
    expect(velocity.v_mps).toBe(0);
    expect(Math.abs(velocity.yaw_rate_rps)).toBe(0.5);
    expect(steeringVelocity(0.01, 0)).toEqual({ v_mps: 0.15, yaw_rate_rps: 0 });
    expect(steeringVelocity(0.05, 0)).toEqual({ v_mps: 0, yaw_rate_rps: -0.1 });
  });
  it("completes a half-turn within 0.02 radians before straight forward travel", () => {
    let yaw = 0;
    let velocity = steeringVelocity(yaw, Math.PI);
    for (let pulse = 0; pulse < 150 && velocity.v_mps === 0; pulse++) {
      yaw += velocity.yaw_rate_rps * 0.1;
      velocity = steeringVelocity(yaw, Math.PI);
    }
    expect(velocity).toEqual({ v_mps: 0.15, yaw_rate_rps: 0 });
    expect(Math.abs(Math.PI - yaw)).toBeLessThan(0.02);
  });
  it("takes the shortest turn across the angle wrap", () => {
    const velocity = steeringVelocity(Math.PI - 0.2, -Math.PI + 0.2);
    expect(velocity.v_mps).toBe(0);
    expect(velocity.yaw_rate_rps).toBeGreaterThan(0);
    expect(velocity.yaw_rate_rps).toBeLessThanOrEqual(0.5);
  });
  it("holds the opposite target through live pose updates and repeated keydown", async () => {
    vi.useFakeTimers();
    let yaw = 0;
    const sent: SteeringVelocity[] = [];
    const steering = new DirectionalSteering(
      async (body) => {
        sent.push(body);
      },
      () => ({ yaw, ready: true }),
      () => {},
    );
    steering.start("down");
    await vi.advanceTimersByTimeAsync(100);
    expect(sent[0]).toEqual({ v_mps: 0, yaw_rate_rps: 0.5 });
    yaw = Math.PI / 2;
    steering.start("down");
    await vi.advanceTimersByTimeAsync(100);
    expect(sent.at(-1)).toEqual({ v_mps: 0, yaw_rate_rps: 0.5 });
    yaw = Math.PI;
    await vi.advanceTimersByTimeAsync(100);
    expect(sent.at(-1)).toEqual({ v_mps: 0.15, yaw_rate_rps: 0 });
    steering.stop();
    const count = sent.length;
    await vi.advanceTimersByTimeAsync(1000);
    expect(sent).toHaveLength(count);
    expect(sent.at(-1)).toEqual({ v_mps: 0, yaw_rate_rps: 0 });
  });
  it("latches a new relative target on direction changes and after release", async () => {
    vi.useFakeTimers();
    let yaw = 0;
    const sent: SteeringVelocity[] = [];
    const steering = new DirectionalSteering(
      async (body) => {
        sent.push(body);
      },
      () => ({ yaw, ready: true }),
      () => {},
    );
    steering.start("left");
    await vi.advanceTimersByTimeAsync(100);
    expect(sent.at(-1)?.yaw_rate_rps).toBe(0.5);
    yaw = Math.PI / 4;
    steering.start("right");
    await vi.advanceTimersByTimeAsync(100);
    expect(sent.at(-1)).toEqual({ v_mps: 0, yaw_rate_rps: -0.5 });
    yaw = -Math.PI / 4;
    await vi.advanceTimersByTimeAsync(100);
    expect(sent.at(-1)).toEqual({ v_mps: 0.15, yaw_rate_rps: 0 });
    steering.stop();
    yaw = 1.2;
    steering.start("up");
    await vi.advanceTimersByTimeAsync(100);
    expect(sent.at(-1)).toEqual({ v_mps: 0.15, yaw_rate_rps: 0 });
    steering.stop();
  });
  it("keeps a held heading independent of camera orbit", async () => {
    vi.useFakeTimers();
    let viewYaw = 0;
    const yaw = 0.79;
    const sent: SteeringVelocity[] = [];
    const steering = new DirectionalSteering(
      async (body) => {
        sent.push(body);
      },
      () => ({ yaw, viewYaw, ready: true }),
      () => {},
    );
    steering.start("up");
    await vi.advanceTimersByTimeAsync(100);
    viewYaw = 2.8;
    await vi.advanceTimersByTimeAsync(100);
    for (const velocity of sent)
      expect(velocity).toEqual({ v_mps: 0.15, yaw_rate_rps: 0 });
    steering.stop();
  });
  it("stops when readiness fails and never queues network pulses", async () => {
    vi.useFakeTimers();
    let ready = true;
    const sent: SteeringVelocity[] = [];
    const steering = new DirectionalSteering(
      async (body) => {
        sent.push(body);
        if (body.v_mps) await new Promise(() => {});
      },
      () => ({ yaw: 0, ready }),
      () => {},
    );
    steering.start("up");
    await vi.advanceTimersByTimeAsync(500);
    expect(sent).toHaveLength(1);
    ready = false;
    await vi.advanceTimersByTimeAsync(100);
    expect(sent.at(-1)).toEqual({ v_mps: 0, yaw_rate_rps: 0 });
  });
});
