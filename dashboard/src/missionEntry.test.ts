import { expect, it } from "vitest";
import { parseMessage } from "./protocol";
const health = {
  version: 1,
  type: "health",
  phone: "ok",
  car: "ok",
  detector: "ok",
  pose_age_ms: 10,
  mode: "explore",
  armed: true,
  stop_reason: null,
};
const entry = {
  session_id: "room",
  map_epoch: 1,
  start: [2, -1],
  frame_id: 42,
  t_capture: 123.5,
  started_at_ms: 123456,
  basis: "explore_start",
};
it("preserves a scoped recorded entry and legacy health without entry metadata", () => {
  expect(parseMessage(health)).toEqual(health);
  expect(parseMessage({ ...health, mission_entry: entry })).toMatchObject({
    mission_entry: entry,
  });
});
it("discards malformed entry metadata without dropping otherwise valid health", () => {
  for (const bad of [
    false,
    {},
    { ...entry, start: [2] },
    { ...entry, start: [NaN, 0] },
    { ...entry, map_epoch: -1 },
    { ...entry, session_id: "" },
    { ...entry, frame_id: 1.5 },
    { ...entry, t_capture: -1 },
    { ...entry, started_at_ms: Infinity },
    { ...entry, basis: "guessed" },
  ]) {
    expect(parseMessage({ ...health, mission_entry: bad })).toEqual({
      ...health,
      mission_entry: null,
    });
  }
});
