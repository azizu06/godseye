import { describe, expect, it } from "vitest";
import { parseMessage, type Health } from "./protocol";
import { explorationPresentation, parseExploration } from "./exploration";

const scan = (extra: Record<string, unknown> = {}) => ({
  phase: "scanning",
  reason: null,
  session_id: "room",
  map_epoch: 1,
  observed_views: 4,
  gained_cells: 21,
  last_gain_cells: 3,
  gained_surface_voxels: 98,
  last_gain_surface_voxels: 12,
  target: [1, 2],
  ...extra,
});
const health = (extra: Record<string, unknown> = {}) => ({
  version: 1,
  type: "health",
  phone: "ok",
  car: "ok",
  detector: "ok",
  mode: "explore",
  armed: true,
  pose_age_ms: 1,
  stop_reason: null,
  exploration: scan(),
  ...extra,
});
const present = (
  extra: Record<string, unknown> = {},
  stale = false,
  key = '["room",1]',
) => explorationPresentation(parseMessage(health(extra)) as Health, stale, key);

describe("additive exploration telemetry", () => {
  it("accepts bounded phases, scoped counts, and measured targets", () => {
    expect(parseExploration(scan())).toEqual(scan());
    expect(
      parseExploration(
        scan({
          phase: "idle",
          session_id: null,
          map_epoch: null,
          target: null,
        }),
      ),
    ).not.toBeNull();
  });
  it("rejects malformed scan fields without dropping critical health", () => {
    for (const extra of [
      { phase: "invented" },
      { observed_views: -1 },
      { gained_cells: Infinity },
      { target: [1, NaN] },
      { reason: "x".repeat(257) },
      { map_epoch: null },
      { last_gain_surface_voxels: -2 },
    ]) {
      expect(parseExploration(scan(extra))).toBeNull();
      const parsed = parseMessage(health({ exploration: scan(extra) }));
      expect(parsed?.type).toBe("health");
      if (parsed?.type === "health") {
        expect(parsed.armed).toBe(true);
        expect(parsed.exploration).toBeUndefined();
      }
    }
  });
  it("keeps older health messages compatible and does not mutate the input", () => {
    const old = health();
    delete (old as Partial<typeof old>).exploration;
    expect(parseMessage(old)?.type).toBe("health");
    const input = health({ exploration: scan({ phase: "bad" }) });
    parseMessage(input);
    expect(input.exploration).toEqual(scan({ phase: "bad" }));
  });
});
describe("truthful scan presentation", () => {
  it("shows actual phases and measured progress without invented completeness", () => {
    for (const phase of [
      "selecting",
      "moving",
      "aligning",
      "settling",
      "scanning",
    ])
      expect(
        present({ exploration: scan({ phase }) })?.title.toLowerCase(),
      ).toContain(phase);
    expect(present()?.progress).toBe("4 stable views · 98 new surface voxels");
    expect(JSON.stringify(present())).not.toMatch(/%|room complete/i);
  });
  it("distinguishes accessible exhaustion from a partial or blocked scan", () => {
    const done = present({
      armed: false,
      exploration: scan({
        phase: "complete",
        reason: "scan_accessible_exhausted",
      }),
    });
    expect(done?.title).toBe("Accessible scan done");
    expect(done?.detail).toContain("Unseen areas may remain");
    expect(
      present({
        armed: false,
        exploration: scan({ phase: "blocked", reason: "sensing_stale" }),
      })?.detail,
    ).toContain("fresh depth");
    expect(
      present({
        exploration: scan({ phase: "complete", reason: "scan_budget" }),
      })?.title,
    ).not.toBe("Accessible scan done");
  });
  it("never reports movement or completion from stale or another map's status", () => {
    expect(present({}, true)?.title).toBe("Explore status stale");
    expect(present({}, false, '["room",2]')?.title).toBe(
      "Waiting for scan status",
    );
    expect(present({ armed: false, stop_reason: "operator_stop" })?.title).toBe(
      "Explore stopped",
    );
    expect(present({ mode: "manual" })).toBeNull();
    expect(explorationPresentation(null, false, null)).toBeNull();
  });
});
