import { describe, expect, it } from "vitest";
import { mapScope, parseRoute } from "./approachRoute";

const expected = {
  session_id: "room",
  map_epoch: 1,
  object_id: "person-1",
  start: [3, 0.5] as [number, number],
};
const assumptions = {
  walker_radius_m: 0.25,
  margin_m: 0.05,
  person_keep_out_m: 0.3,
  approach_reach_m: 1.5,
  unknown: "blocked",
  doors: "not_inferred",
  verified: false,
};
const ok = {
  version: 1,
  ...expected,
  person: [2, 3],
  occupancy_revision: 7,
  status: "ok",
  points: [
    [3, 0.5],
    [1, 2],
    [1.6, 2.4],
  ],
  approach: [1.6, 2.4],
  length_m: 3.2,
  assumptions,
};

describe("approach route response", () => {
  it("accepts an unverified route for exactly this map, person and start", () => {
    expect(parseRoute(ok, expected)?.status).toBe("ok");
    expect(
      parseRoute(
        {
          ...ok,
          status: "unavailable",
          reason: "no_observed_free_route",
          points: undefined,
        },
        expected,
      ),
    ).toEqual({
      status: "unavailable",
      reason: "no_observed_free_route",
      assumptions,
    });
  });

  it("rejects routes for another map, person or start, or claimed as verified", () => {
    for (const bad of [
      { ...ok, map_epoch: 2 },
      { ...ok, session_id: "other" },
      { ...ok, object_id: "person-2" },
      { ...ok, start: [0, 0] },
      { ...ok, assumptions: { ...assumptions, verified: true } },
      { ...ok, assumptions: { ...assumptions, unknown: "traversable" } },
      { ...ok, points: [[3, 0.5]] },
      {
        ...ok,
        points: [
          [3, 0.5],
          [Number.NaN, 1],
        ],
      },
    ])
      expect(parseRoute(bad, expected)).toBeNull();
  });

  it("reads the active map scope from its key", () => {
    expect(mapScope(JSON.stringify(["room", 1]))).toEqual({
      session_id: "room",
      map_epoch: 1,
    });
    expect(mapScope(null)).toBeNull();
  });
});
