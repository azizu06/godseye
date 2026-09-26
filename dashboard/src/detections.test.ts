import { describe, expect, it } from "vitest";
import { parseMessage } from "./protocol";
import { emptyMission, reduceMessage } from "./state";
import {
  DETECTION_STALE_MS,
  boxPercent,
  detectionImageUrl,
  liveMarkers,
} from "./detections";

const scope = { session_id: "room", map_epoch: 1 };
const wire = (overrides: Record<string, unknown> = {}) => ({
  version: 1,
  type: "detections",
  ...scope,
  frame_id: 7,
  t_capture: 7,
  t_wall_ms: 1_700_000_000_000,
  image: { width: 80, height: 60 },
  source: "backend_detector",
  classes: ["person", "backpack"],
  detections: [
    {
      class: "person",
      confidence: 0.87,
      box: [40, 10, 56, 50],
      position: [-1, 2, 2.6],
      depth_m: 2,
      object_id: "person-1",
    },
    {
      class: "backpack",
      confidence: 0.71,
      box: [4, 24, 16, 36],
      position: null,
      depth_m: null,
      object_id: null,
    },
  ],
  ...overrides,
});

describe("detections wire", () => {
  it("accepts received detector output with and without measured positions", () => {
    const message = parseMessage(JSON.stringify(wire()));
    expect(message?.type).toBe("detections");
  });

  it("rejects boxes outside their image, unscoped or non-detector sources", () => {
    const [person] = wire().detections as Record<string, unknown>[];
    for (const bad of [
      wire({ detections: [{ ...person, box: [40, 10, 81, 50] }] }),
      wire({ detections: [{ ...person, box: [56, 10, 40, 50] }] }),
      wire({ detections: [{ ...person, position: [1, 2] }] }),
      wire({ detections: [{ ...person, confidence: 1.2 }] }),
      wire({ source: "simulator" }),
      wire({ session_id: undefined, map_epoch: undefined }),
      wire({ session_id: null, map_epoch: null }),
    ])
      expect(parseMessage(bad)).toBeNull();
  });
});

describe("live detection state", () => {
  const received = (now: number) =>
    reduceMessage(emptyMission(), parseMessage(wire())!, now);

  it("places only depth-measured detections, and only while fresh", () => {
    const mission = received(1000);
    const markers = liveMarkers(mission.detections, 1000);
    expect(markers.map((m) => [m.class, m.position, m.object_id])).toEqual([
      ["person", [-1, 2, 2.6], "person-1"],
    ]);
    expect(
      liveMarkers(mission.detections, 1000 + DETECTION_STALE_MS + 1),
    ).toEqual([]);
  });

  it("a map reset drops the previous map's detections", () => {
    const mission = reduceMessage(
      received(1000),
      parseMessage({
        version: 1,
        type: "objects",
        session_id: "room",
        map_epoch: 2,
        objects: [],
      })!,
      1100,
    );
    expect(mission.detections).toBeNull();
    expect(liveMarkers(mission.detections, 1100)).toEqual([]);
  });

  it("maps boxes onto their own image and asks for that exact frame", () => {
    expect(boxPercent([40, 15, 60, 45], { width: 80, height: 60 })).toEqual({
      left: 50,
      top: 25,
      width: 25,
      height: 50,
    });
    expect(detectionImageUrl("http://mac:8765/", received(0).detections!)).toBe(
      "http://mac:8765/capture/detections.jpg?session_id=room&map_epoch=1&frame_id=7",
    );
  });
});
