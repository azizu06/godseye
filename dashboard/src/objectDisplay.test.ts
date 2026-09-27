import { describe, expect, it } from "vitest";
import type { WorldObject } from "./protocol";
import {
  OBJECT_STALE_S,
  displayedObjects,
  labelPriority,
  objectEvidence,
  objectStateText,
  overlappingLabels,
} from "./objectDisplay";

const obj = (over: Partial<WorldObject> = {}): WorldObject => ({
  id: "o",
  class: "chair",
  position: [0, 0, 0],
  confidence: 0.8,
  first_seen: 1000,
  last_seen: 1000,
  observations: 5,
  state: "present",
  ...over,
});

describe("object display evidence", () => {
  it("needs two frames and 0.5 confidence to be strong", () => {
    expect(objectEvidence(obj())).toBe("strong");
    expect(objectEvidence(obj({ observations: 1 }))).toBe("weak");
    expect(objectEvidence(obj({ confidence: 0.39 }))).toBe("weak");
    expect(objectEvidence(obj({ observations: 2, confidence: 0.5 }))).toBe(
      "strong",
    );
  });

  it("keeps a once-seen low-confidence person as a possible person", () => {
    const p = obj({ class: "person", observations: 1, confidence: 0.27 });
    expect(objectEvidence(p)).toBe("possible_person");
    expect(displayedObjects([p], false, null)).toEqual([p]);
  });

  it("hides weak non-person objects by default but keeps them in raw view", () => {
    const frisbee = obj({
      id: "f",
      class: "frisbee",
      observations: 2,
      confidence: 0.39,
    });
    const chair = obj({ id: "c" });
    expect(displayedObjects([frisbee, chair], false, null)).toEqual([chair]);
    expect(displayedObjects([frisbee, chair], true, null)).toEqual([
      frisbee,
      chair,
    ]);
  });

  it("always draws the selected object", () => {
    const frisbee = obj({ id: "f", class: "frisbee", observations: 1 });
    expect(displayedObjects([frisbee], false, "f")).toEqual([frisbee]);
  });
});

describe("object state wording", () => {
  it("reads present only while recently re-observed", () => {
    const o = obj({ last_seen: 1000 });
    expect(objectStateText(o, 1000_000 + 5_000)).toBe("Present");
    expect(objectStateText(o, (1000 + OBJECT_STALE_S + 1) * 1000)).toBe(
      "Last seen 31s ago",
    );
    expect(objectStateText(o, (1000 + 540) * 1000)).toBe("Last seen 9m ago");
  });

  it("keeps backend change states", () => {
    expect(objectStateText(obj({ state: "moved" }), 9e12)).toBe("Moved");
    expect(objectStateText(obj({ state: "not_found_on_rescan" }), 0)).toBe(
      "Not found on rescan",
    );
  });
});

describe("label decluttering", () => {
  const rect = (id: string, x: number, priority: number) => ({
    id,
    x,
    y: 0,
    width: 60,
    height: 20,
    priority,
  });

  it("hides the lower-priority label of an overlapping pair", () => {
    expect(overlappingLabels([rect("low", 10, 1), rect("high", 0, 5)])).toEqual(
      new Set(["low"]),
    );
  });

  it("keeps separated labels and never hides infinite priority", () => {
    expect(overlappingLabels([rect("a", 0, 1), rect("b", 200, 1)]).size).toBe(
      0,
    );
    expect(
      overlappingLabels([rect("pinned", 5, Infinity), rect("x", 0, 50)]),
    ).toEqual(new Set(["x"]));
  });

  it("ranks a weak person above a strong object", () => {
    expect(
      labelPriority(obj({ class: "person", confidence: 0.3, observations: 1 })),
    ).toBeGreaterThan(
      labelPriority(obj({ confidence: 0.95, observations: 10 })),
    );
  });
});
