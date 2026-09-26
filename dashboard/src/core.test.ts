import { describe, expect, it } from "vitest";
import { parseMessage } from "./protocol";
import { emptyMission, reduceMessage } from "./state";
import { Simulator } from "./simulator";

describe("wire data boundary", () => {
  it("rejects nonfinite or incomplete poses and unknown versions", () => {
    expect(
      parseMessage({
        version: 1,
        type: "pose",
        position: [0, NaN, 0],
        yaw_rad: 0,
        tracking: "normal",
      }),
    ).toBeNull();
    expect(parseMessage({ version: 2, type: "path", points: [] })).toBeNull();
    expect(parseMessage({ version: 1, type: "future" })).toBeNull();
  });
  it("validates occupancy bytes against grid dimensions and cell values", () => {
    const grid = {
      version: 1,
      type: "occupancy",
      origin: [0, 0],
      cell_m: 0.05,
      width: 2,
      height: 2,
    };
    expect(parseMessage({ ...grid, cells: "AAECAQ==" })).not.toBeNull();
    expect(parseMessage({ ...grid, cells: "AAE=" })).toBeNull();
    expect(parseMessage({ ...grid, cells: "AAECAw==" })).toBeNull();
  });
  it("rejects object states and mismatched point colors", () => {
    expect(
      parseMessage({
        version: 1,
        type: "points",
        chunk_id: 1,
        positions: [0, 0, 0],
        colors: [],
      }),
    ).toBeNull();
    expect(
      parseMessage({
        version: 1,
        type: "objects",
        objects: [
          {
            id: "a",
            class: "chair",
            position: [0, 0, 0],
            confidence: 2,
            state: "present",
          },
        ],
      }),
    ).toBeNull();
  });
});
describe("bounded mission memory", () => {
  it("does not append duplicate chunks and evicts oldest chunks at the limit", () => {
    let state = emptyMission();
    const msg = {
      version: 1 as const,
      type: "points" as const,
      chunk_id: 1,
      positions: [1, 2, 3],
      colors: [1, 1, 1],
    };
    state = reduceMessage(state, msg, 1);
    state = reduceMessage(state, msg, 2);
    expect(state.chunks).toHaveLength(1);
    for (let i = 2; i <= 130; i++)
      state = reduceMessage(state, { ...msg, chunk_id: i }, i);
    expect(state.chunks).toHaveLength(120);
    expect(state.chunks[0].chunk_id).toBe(11);
  });
  it("caps total retained points even for large chunks", () => {
    let state = emptyMission();
    for (let i = 0; i < 4; i++)
      state = reduceMessage(
        state,
        {
          version: 1,
          type: "points",
          chunk_id: i,
          positions: Array(60000).fill(0),
          colors: Array(60000).fill(0.5),
        },
        i,
      );
    expect(state.chunks.reduce((n, c) => n + c.positions.length / 3, 0)).toBe(
      60000,
    );
  });
  it("deduplicates events and preserves history through object snapshots", () => {
    const event = {
      version: 1 as const,
      type: "event" as const,
      kind: "moved" as const,
      object_id: "bag",
      old_position: [0, 0, 0] as [number, number, number],
      new_position: [1, 0, 0] as [number, number, number],
      displacement_m: 1,
      t: 5,
    };
    let state = reduceMessage(emptyMission(), event, 1);
    state = reduceMessage(state, event, 2);
    state = reduceMessage(
      state,
      { version: 1, type: "objects", objects: [] },
      3,
    );
    expect(state.events).toHaveLength(1);
  });
});
describe("operator simulation", () => {
  it("starts disarmed, requires explicit arming and stops on mode switch", () => {
    const sim = new Simulator();
    expect(sim.health.armed).toBe(false);
    expect(() =>
      sim.command("/manual", { v_mps: 0.1, yaw_rate_rps: 0 }),
    ).toThrow(/arm/i);
    sim.command("/arm");
    expect(sim.health.armed).toBe(true);
    sim.command("/mode", { mode: "navigate" });
    expect(sim.health.armed).toBe(false);
  });
  it("expires a manual lease and disarms on tracking loss", () => {
    const sim = new Simulator();
    sim.command("/arm");
    sim.command("/manual", { v_mps: 0.1, yaw_rate_rps: 0 });
    sim.tick(0.1);
    const moved = [...sim.position];
    sim.tick(0.4);
    sim.tick(0.1);
    expect(sim.position).toEqual(moved);
    sim.setTracking(false);
    expect(sim.health.armed).toBe(false);
    expect(() => sim.command("/arm")).toThrow(/tracking/i);
  });
  it("rescans to one confirmed relocation with stable identity and displacement", () => {
    const sim = new Simulator();
    const old = [...sim.objects[0].position];
    sim.command("/rescan");
    sim.tick(3.1);
    const events = sim.snapshot().filter((m) => m.type === "event");
    const move = events.find((m) => m.type === "event" && m.kind === "moved");
    expect(move).toMatchObject({
      object_id: "sim-backpack",
      old_position: old,
      displacement_m: 1.6,
    });
    expect(sim.objects[0].state).toBe("moved");
    sim.command("/stop");
    expect(sim.health.armed).toBe(false);
  });
  it("resets all accumulated mission data on a new session", () => {
    const sim = new Simulator();
    sim.command("/rescan");
    sim.tick(4);
    sim.command("/session");
    expect(sim.objects[0].state).toBe("present");
    expect(sim.health.armed).toBe(false);
    expect(sim.elapsed).toBe(0);
  });
});

describe("simulated motion consistency", () => {
  it("explores from the current position without teleporting or exceeding 0.2m/s", () => {
    const sim = new Simulator();
    sim.command("/mode", { mode: "explore" });
    sim.command("/arm");
    const before = [...sim.position];
    sim.tick(0.1);
    expect(
      Math.hypot(sim.position[0] - before[0], sim.position[2] - before[2]),
    ).toBeLessThanOrEqual(0.020001);
    expect(
      Math.hypot(sim.position[0] - before[0], sim.position[2] - before[2]),
    ).toBeGreaterThan(0);
  });
  it("cannot rearm during a rescan and tracking loss cancels change claims", () => {
    const sim = new Simulator();
    sim.command("/rescan");
    expect(() => sim.command("/arm")).toThrow(/rescan/i);
    sim.setTracking(false);
    sim.tick(4);
    expect(sim.events.filter((e) => e.kind === "moved")).toHaveLength(0);
  });
});
