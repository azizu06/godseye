import { expect, it } from "vitest";
import { parseMessage, parseEventHistory } from "./protocol";
import { emptyMission, reduceMessage } from "./state";
const scope = { session_id: "room", map_epoch: 1 };
const point = {
  version: 1,
  type: "points",
  ...scope,
  chunk_id: 1,
  positions: [1, 0, 2],
  colors: [0.2, 0.3, 0.4],
};
const event = {
  version: 1,
  type: "event",
  ...scope,
  id: "event-7",
  rescan_id: "rescan-2",
  kind: "possible_move",
  object_id: "old",
  new_object_id: "new",
  t: 10,
};
it("rejects malformed scope and event identity but accepts legacy messages", () => {
  expect(parseMessage({ ...point, map_epoch: "1" })).toBeNull();
  expect(parseMessage({ ...point, session_id: undefined })).toBeNull();
  expect(parseMessage({ ...event, id: 7 })).toBeNull();
  expect(parseMessage({ ...event, new_object_id: 42 })).toBeNull();
  expect(
    parseMessage({ ...point, session_id: undefined, map_epoch: undefined }),
  ).not.toBeNull();
});
it("clears all spatial memory before accepting the first chunk of a new map", () => {
  let s = reduceMessage(emptyMission(), parseMessage(point)!);
  s = reduceMessage(
    s,
    parseMessage({
      version: 1,
      type: "pose",
      position: [1, 0, 2],
      yaw_rad: 0,
      tracking: "normal",
    })!,
  );
  s = reduceMessage(s, parseMessage(event)!);
  s = reduceMessage(
    s,
    parseMessage({ ...point, map_epoch: 2, positions: [9, 0, 8] })!,
  );
  expect(s.chunks).toHaveLength(1);
  expect(s.chunks[0].positions).toEqual([9, 0, 8]);
  expect(s.pose).toBeNull();
  expect(s.events).toHaveLength(0);
  expect(s.trajectory).toHaveLength(0);
});
it("hydrates matching event history and deduplicates overlap by stable id in time order", () => {
  const key = JSON.stringify(["room", 1]);
  const history = parseEventHistory(
    {
      version: 1,
      ...scope,
      events: [event, { ...event, id: "event-6", t: 5 }],
    },
    key,
  )!;
  let s = reduceMessage(emptyMission(), parseMessage(event)!);
  for (const e of history) s = reduceMessage(s, e);
  expect(s.events.map((e) => e.id)).toEqual(["event-6", "event-7"]);
  expect(s.events[1].new_object_id).toBe("new");
  expect(
    parseEventHistory(
      { version: 1, ...scope, map_epoch: 2, events: [event] },
      key,
    ),
  ).toBeNull();
  expect(
    parseEventHistory(
      { version: 1, ...scope, events: [{ ...event, t: NaN }] },
      key,
    ),
  ).toBeNull();
});
