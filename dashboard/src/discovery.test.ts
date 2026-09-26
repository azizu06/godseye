import { expect, it } from "vitest";
import { Simulator, makeRoomPoints } from "../tests/fixtures/simulator";
import { decodeCells } from "./protocol";
import { emptyMission, reduceMessage } from "./state";
it("starts unknown and accumulates only newly observed points within range", () => {
  const sim = new Simulator();
  let state = sim
    .snapshot(true)
    .reduce((s, m) => reduceMessage(s, m), emptyMission());
  expect(state.chunks).toHaveLength(0);
  expect(state.objects).toHaveLength(0);
  expect([...decodeCells(state.occupancy!)].every((v) => v === 0)).toBe(true);
  for (let i = 0; i < 20; i++) {
    sim.tick(0.1);
    state = sim.snapshot().reduce((s, m) => reduceMessage(s, m), state);
  }
  const positions = state.chunks.flatMap((c) => c.positions);
  expect(positions.length).toBeGreaterThan(0);
  expect(positions.length).toBeLessThan(makeRoomPoints().positions.length);
  for (let i = 0; i < positions.length; i += 3)
    expect(
      Math.hypot(
        positions[i] - sim.position[0],
        positions[i + 1] - sim.position[1],
        positions[i + 2] - sim.position[2],
      ),
    ).toBeLessThanOrEqual(5);
  expect(state.objects.length).toBeGreaterThan(0);
  expect(state.objects.length).toBeLessThan(5);
  const saved = positions.length;
  sim.yaw += Math.PI;
  for (let i = 0; i < 20; i++) {
    sim.tick(0.1);
    state = sim.snapshot().reduce((s, m) => reduceMessage(s, m), state);
  }
  expect(state.chunks.flatMap((c) => c.positions).length).toBeGreaterThan(
    saved,
  );
  sim.setTracking(false);
  sim.tick(1);
  expect(sim.snapshot().some((m) => m.type === "points")).toBe(false);
  sim.command("/session");
  expect(sim.snapshot(true).filter((m) => m.type === "points")).toHaveLength(0);
});

it("retains discovered surfaces through a long exploration and rejects an occluded demo", () => {
  const sim = new Simulator();
  let state = emptyMission();
  let count = 0;
  sim.command("/mode", { mode: "explore" });
  sim.command("/arm");
  for (let i = 0; i < 1000; i++) {
    sim.tick(0.1);
    state = sim.snapshot().reduce((s, m) => reduceMessage(s, m), state);
    const next = state.chunks.reduce((n, c) => n + c.positions.length / 3, 0);
    expect(next).toBeGreaterThanOrEqual(count);
    count = next;
  }
  expect(state.chunks.length).toBeLessThan(120);
  sim.command("/stop");
  sim.position = [3.7, 0.16, -2.7];
  expect(() => sim.command("/rescan")).toThrow(/clear view/);
  expect(sim.events.some((e) => e.kind === "moved")).toBe(false);
});
