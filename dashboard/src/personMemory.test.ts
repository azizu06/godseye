import { describe, expect, it } from "vitest";
import { DepthContradiction, type DepthObservation } from "./depthRetirement";
import { parseMessage, type DetectionFrame, type Vec3 } from "./protocol";
import {
  clearMissionPeople,
  emptyMission,
  reconnectMission,
  reduceMessage,
} from "./state";
import {
  clearPeople,
  displayedStoredObjects,
  emptyPeople,
  MAX_RETAINED_PEOPLE,
  observePeople,
  personProofResults,
  retainedPeople,
  type PersonMemory,
} from "./personMemory";
import type { WorldObject } from "./protocol";

const scope = { session_id: "hall", map_epoch: 1 };
let frameId = 0;
const frame = (
  t: number,
  people: (Vec3 | null)[],
  objectIds: (string | null)[] = [],
): DetectionFrame & typeof scope => ({
  ...scope,
  frame_id: ++frameId,
  t_capture: t,
  t_wall_ms: 1_700_000_000_000 + t * 1000,
  image: { width: 80, height: 60 },
  source: "backend_detector",
  classes: ["person"],
  detections: people.map((position, i) => ({
    class: "person",
    confidence: 0.8,
    box: [10, 10, 20, 50],
    position,
    depth_m: position ? 2 : null,
    object_id: objectIds[i] ?? null,
  })),
});
const observe = (
  memory: PersonMemory,
  t: number,
  people: (Vec3 | null)[],
  objectIds: (string | null)[] = [],
) => observePeople(memory, frame(t, people, objectIds), t * 1000);
const positions = (memory: PersonMemory) =>
  memory.tracks.map((track) => track.position);

describe("retained last-measured people", () => {
  it("keeps the latest measured position after an empty frame, look-away and a long unseen interval", () => {
    let memory = observe(emptyPeople(), 1, [[0, 1, -2]]);
    const seen = memory.tracks[0];
    // Camera turned away: detector frames with nobody, or with no placed person.
    memory = observe(memory, 1.5, []);
    memory = observe(memory, 2, [null]);
    memory = observePeople(memory, { ...frame(2.5, []), detections: [] }, 2500);
    expect(memory.tracks).toEqual([seen]);
    // A day later nothing has removed it: time alone is not evidence of departure.
    const received = { frame: frame(2.6, []), receivedAt: 2_600 };
    expect(retainedPeople(memory, received, 86_400_000)).toEqual([seen]);
  });

  it("follows one walking person to the latest measurement without a ghost trail", () => {
    let memory = emptyPeople();
    for (const [t, x] of [
      [1, 0],
      [1.5, 0.4],
      [2, 0.8],
      [2.5, 1.3],
      [3, 1.9],
    ])
      memory = observe(memory, t, [[x, 1, -2]]);
    // Backend running means would give [0.2..] plus new records; the display keeps one latest.
    expect(positions(memory)).toEqual([[1.9, 1, -2]]);
    expect(memory.tracks[0].sightings).toBe(5);
    expect(memory.tracks[0].measuredAt).toBe(3);
  });

  it("ignores repeated and out-of-order detector frames", () => {
    let memory = observe(emptyPeople(), 2, [[1, 1, -2]]);
    const repeated = observe(memory, 2, [[5, 1, -2]]);
    expect(repeated).toBe(memory);
    memory = observe(memory, 1.5, [[0.9, 1, -2]]);
    expect(positions(memory)).toEqual([[1, 1, -2]]);
  });

  it("keeps two people separate and never merges or deletes on local ambiguity", () => {
    let memory = observe(emptyPeople(), 1, [
      [0, 1, -2],
      [1.2, 1, -2],
    ]);
    memory = observe(memory, 1.5, [
      [0.3, 1, -2.2],
      [1.5, 1, -2.1],
    ]);
    expect(positions(memory)).toEqual([
      [0.3, 1, -2.2],
      [1.5, 1, -2.1],
    ]);
    // One detection between them: the nearer track follows, the other is retained.
    memory = observe(memory, 2, [[0.95, 1, -2.1]]);
    expect(memory.tracks).toHaveLength(2);
    expect(positions(memory)).toContainEqual([0.3, 1, -2.2]);
    expect(positions(memory)).toContainEqual([0.95, 1, -2.1]);
  });

  it("does not move a long-unseen person to a different sighting; the old location needs its own proof", () => {
    let memory = observe(emptyPeople(), 1, [[0, 1, -2]]);
    memory = observe(memory, 30, [[1.2, 1, -2]]);
    expect(positions(memory)).toEqual([
      [0, 1, -2],
      [1.2, 1, -2],
    ]);
    // Within the tight same-place radius a returning person updates the old marker.
    memory = observe(memory, 60, [[0.3, 1, -2]]);
    expect(memory.tracks).toHaveLength(2);
    expect(positions(memory)).toContainEqual([0.3, 1, -2]);
  });

  it("bounds memory by retiring only the oldest measurement past the cap", () => {
    let memory = emptyPeople();
    for (let i = 0; i <= MAX_RETAINED_PEOPLE; i++)
      memory = observe(memory, i + 1, [[i * 3, 1, -2]]);
    expect(memory.tracks).toHaveLength(MAX_RETAINED_PEOPLE);
    expect(positions(memory)).not.toContainEqual([0, 1, -2]);
    expect(positions(memory)).toContainEqual([3, 1, -2]);
  });

  it("is reset only by a map lifecycle change and kept across a same-map reconnect", () => {
    const wire = { version: 1, type: "detections", ...frame(1, [[0, 1, -2]]) };
    let state = reduceMessage(emptyMission(), parseMessage(wire)!, 1000);
    expect(state.people.tracks).toHaveLength(1);
    state = reconnectMission(state);
    expect(state.people.tracks).toHaveLength(1);
    state = reduceMessage(
      state,
      parseMessage({
        version: 1,
        type: "objects",
        session_id: "next",
        map_epoch: 1,
        objects: [],
      })!,
    );
    expect(state.people.tracks).toHaveLength(0);
  });

  it("removes a marker only for the exact measurement a proof covered", () => {
    let memory = observe(emptyPeople(), 1, [
      [0, 1, -2],
      [3, 1, -2],
    ]);
    const [a, b] = memory.tracks;
    // The second person was re-measured after the proof request: keep it.
    memory = observe(memory, 2, [[3.2, 1, -2]]);
    memory = clearPeople(memory, [
      { id: a.id, measuredAt: a.measuredAt },
      { id: b.id, measuredAt: b.measuredAt },
    ]);
    expect(positions(memory)).toEqual([[3.2, 1, -2]]);
  });

  it("ignores a proof that arrives for a different map", () => {
    const wire = { version: 1, type: "detections", ...frame(1, [[0, 1, -2]]) };
    const state = reduceMessage(emptyMission(), parseMessage(wire)!, 1000);
    const [track] = state.people.tracks;
    const proof = [{ id: track.id, measuredAt: track.measuredAt }];
    expect(clearMissionPeople(state, '["old",1]', proof)).toBe(state);
    expect(
      clearMissionPeople(state, state.mapKey!, proof).people.tracks,
    ).toEqual([]);
  });

  it("is not live-drawn twice: the current LIVE frame owns a person until it expires", () => {
    const current = frame(1, [[0, 1, -2]]);
    const memory = observePeople(emptyPeople(), current, 1000);
    const received = { frame: current, receivedAt: 1000 };
    expect(retainedPeople(memory, received, 1500)).toEqual([]);
    // LIVE expires after its stale window; the retained marker takes over.
    expect(retainedPeople(memory, received, 5000)).toEqual(memory.tracks);
    // A newer empty frame ends LIVE immediately, the retained marker stays.
    const empty = { frame: frame(1.5, []), receivedAt: 1500 };
    expect(retainedPeople(memory, empty, 1500)).toEqual(memory.tracks);
  });

  it("hides backend person records represented by a retained marker, never other objects", () => {
    const object = (id: string, name: string): WorldObject => ({
      id,
      class: name,
      position: [0, 1, -2],
      confidence: 0.9,
      first_seen: 1,
      last_seen: 1,
      observations: 3,
      state: "present",
    });
    let memory = observe(emptyPeople(), 1, [[0, 1, -2]], ["avg-1"]);
    memory = observe(memory, 1.5, [[0.8, 1, -2]], ["avg-2"]);
    const objects = [
      object("avg-1", "person"),
      object("avg-2", "person"),
      object("other", "person"),
      object("bag", "backpack"),
    ];
    expect(
      displayedStoredObjects(objects, memory, null).map((o) => o.id),
    ).toEqual(["other", "bag"]);
    // A selected record is always drawn.
    expect(
      displayedStoredObjects(objects, memory, "avg-1").map((o) => o.id),
    ).toEqual(["avg-1", "other", "bag"]);
  });
});

const observation = (t: number, cameraX = 0, depth = 3): DepthObservation => ({
  sessionId: "hall",
  mapEpoch: 1,
  capturedAt: t,
  width: 20,
  height: 16,
  depth: new Float32Array(320).fill(depth),
  confidence: new Uint8Array(320).fill(2),
  projection: {
    transform: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, cameraX, 0, 0, 1],
    intrinsics: [40, 0, 0, 0, 40, 0, 40, 32, 1],
    imageWidth: 80,
    imageHeight: 64,
  },
});

describe("positive clearance of a remembered person", () => {
  const person = { id: "p1", measuredAt: 1, position: [0, 0, -1] as Vec3 };
  const proof = (first: DepthObservation, second: DepthObservation) =>
    personProofResults(new DepthContradiction([first, second]), [person]);

  it("clears a person only when two newer reliable views see through the remembered extent", () => {
    expect(proof(observation(2), observation(3))).toEqual([
      { id: "p1", measuredAt: 1 },
    ]);
  });

  it("keeps the person when the camera looks away, depth is unreliable, or something nearer blocks the view", () => {
    expect(proof(observation(2, 10), observation(3, 10))).toEqual([]);
    const hole = observation(3);
    hole.confidence[8 * 20 + 10] = 1;
    expect(proof(observation(2), hole)).toEqual([]);
    const missing = observation(3);
    missing.depth[8 * 20 + 10] = NaN;
    expect(proof(observation(2), missing)).toEqual([]);
    // Still standing there (or an occluder in front): depth is not farther.
    expect(proof(observation(2), observation(3, 0, 1))).toEqual([]);
    expect(proof(observation(2), observation(3, 0, 0.6))).toEqual([]);
  });

  it("never uses views captured before or at the person's latest measurement", () => {
    const late = { ...person, measuredAt: 2 };
    expect(
      personProofResults(
        new DepthContradiction([observation(2), observation(3)]),
        [late],
      ),
    ).toEqual([]);
  });
});
