import type { DepthContradiction } from "./depthRetirement";
import { detectionsLive } from "./detections";
import type { DetectionFrame, Vec3, WorldObject } from "./protocol";
import type { ReceivedDetections } from "./state";

/**
 * Display-only memory of where people were last measured. A person seen by the
 * detector with same-frame depth keeps one marker at the latest measured
 * position until two newer, calibrated, reliable depth views see through that
 * spot (the surface worker's DepthContradiction). Looking away, an empty or
 * missed detector frame, occlusion, disconnects and elapsed time never remove
 * it. A cleared marker means that old location was observed empty, not that
 * the person's current location is known.
 *
 * This is not identity tracking and never feeds navigation, routes or
 * collision evidence. Association is nearest-first and local, so two people
 * crossing can swap markers.
 */
export interface PersonTrack {
  id: string;
  /** Latest same-frame depth-placed position, never a running mean. */
  position: Vec3;
  /** Detector frame `t_capture` (phone uptime seconds), comparable to depth proofs. */
  measuredAt: number;
  /** Viewer receipt time (ms), for age wording. */
  receivedAt: number;
  frameId: number;
  confidence: number;
  sightings: number;
}
export interface PersonMemory {
  tracks: PersonTrack[];
  /** Newest processed detector capture time; repeated or older frames are ignored. */
  latestAt: number;
  nextId: number;
  /** Backend person records linked by detections; represented here, not redrawn. */
  linkedObjectIds: string[];
}
export interface PersonProbe {
  id: string;
  measuredAt: number;
  position: Vec3;
}
export interface PersonClearance {
  id: string;
  measuredAt: number;
}

/** Same-place radius, matching the backend object association radius. */
export const PERSON_SAME_PLACE_M = 0.5;
/** A person re-detected within this many seconds is followed as they walk. */
export const PERSON_FOLLOW_S = 2;
/** Generous walking speed used only to widen the follow radius. */
const PERSON_FOLLOW_MPS = 1.5;
const PERSON_FOLLOW_MAX_M = 2;
/** Bounded memory: past this many retained people the oldest measurement retires. */
export const MAX_RETAINED_PEOPLE = 64;
const MAX_LINKED_OBJECTS = 2000;
/**
 * Conservative body extent around the measured surface point. Every corner
 * must be in view and seen through in both proof views.
 */
export const PERSON_HALF_EXTENT: Vec3 = [0.25, 0.35, 0.25];

export const emptyPeople = (): PersonMemory => ({
  tracks: [],
  latestAt: -Infinity,
  nextId: 1,
  linkedObjectIds: [],
});

const followRadius = (dt: number) =>
  dt <= PERSON_FOLLOW_S
    ? Math.min(
        PERSON_FOLLOW_MAX_M,
        PERSON_SAME_PLACE_M + PERSON_FOLLOW_MPS * Math.max(0, dt),
      )
    : PERSON_SAME_PLACE_M;

/** Fold one detector frame into the retained people; empty frames change nothing. */
export function observePeople(
  memory: PersonMemory,
  frame: DetectionFrame,
  receivedAt: number,
): PersonMemory {
  if (!(frame.t_capture > memory.latestAt)) return memory;
  const found = frame.detections.filter(
    (d): d is typeof d & { position: Vec3 } =>
      d.class === "person" && d.position !== null,
  );
  if (!found.length) return { ...memory, latestAt: frame.t_capture };
  const pairs: [number, number, number][] = [];
  memory.tracks.forEach((track, t) => {
    const radius = followRadius(frame.t_capture - track.measuredAt);
    found.forEach((d, i) => {
      const distance = Math.hypot(
        d.position[0] - track.position[0],
        d.position[1] - track.position[1],
        d.position[2] - track.position[2],
      );
      if (distance <= radius) pairs.push([distance, t, i]);
    });
  });
  pairs.sort((a, b) => a[0] - b[0]);
  const tracks = [...memory.tracks];
  const taken = new Set<number>(),
    matched = new Set<number>();
  for (const [, t, i] of pairs) {
    if (taken.has(t) || matched.has(i)) continue;
    taken.add(t);
    matched.add(i);
    const d = found[i];
    tracks[t] = {
      ...tracks[t],
      position: [...d.position],
      measuredAt: frame.t_capture,
      receivedAt,
      frameId: frame.frame_id,
      confidence: d.confidence,
      sightings: tracks[t].sightings + 1,
    };
  }
  let nextId = memory.nextId;
  found.forEach((d, i) => {
    if (matched.has(i)) return;
    tracks.push({
      id: `person-${nextId++}`,
      position: [...d.position],
      measuredAt: frame.t_capture,
      receivedAt,
      frameId: frame.frame_id,
      confidence: d.confidence,
      sightings: 1,
    });
  });
  const linked = new Set(memory.linkedObjectIds);
  for (const d of found) if (d.object_id) linked.add(d.object_id);
  const kept =
    tracks.length > MAX_RETAINED_PEOPLE
      ? new Set(
          [...tracks]
            .sort((a, b) => b.measuredAt - a.measuredAt)
            .slice(0, MAX_RETAINED_PEOPLE),
        )
      : null;
  return {
    tracks: kept ? tracks.filter((track) => kept.has(track)) : tracks,
    latestAt: frame.t_capture,
    nextId,
    linkedObjectIds: [...linked].slice(-MAX_LINKED_OBJECTS),
  };
}

/** Remove markers whose exact latest measurement a positive proof covered. */
export function clearPeople(
  memory: PersonMemory,
  cleared: readonly PersonClearance[],
): PersonMemory {
  const tracks = memory.tracks.filter(
    (track) =>
      !cleared.some(
        (c) => c.id === track.id && c.measuredAt === track.measuredAt,
      ),
  );
  return tracks.length === memory.tracks.length
    ? memory
    : { ...memory, tracks };
}

export function personExtent([x, y, z]: Vec3): Vec3[] {
  const [hx, hy, hz] = PERSON_HALF_EXTENT;
  const corners: Vec3[] = [];
  for (const dx of [-hx, hx])
    for (const dy of [-hy, hy])
      for (const dz of [-hz, hz]) corners.push([x + dx, y + dy, z + dz]);
  return corners;
}

/**
 * People whose whole remembered extent both proof views see through. Both views
 * must be newer than the person's latest measurement. An out-of-view corner,
 * unreliable or missing depth, or anything nearer (including the person) keeps
 * the marker.
 */
export function personProofResults(
  evidence: DepthContradiction,
  people: readonly PersonProbe[],
): PersonClearance[] {
  return people
    .filter(
      (person) =>
        person.measuredAt < evidence.before &&
        evidence.region(personExtent(person.position)),
    )
    .map(({ id, measuredAt }) => ({ id, measuredAt }));
}

/** Retained markers to draw: every person not currently shown by a LIVE frame marker. */
export function retainedPeople(
  memory: PersonMemory,
  detections: ReceivedDetections | null,
  now: number,
): PersonTrack[] {
  const live =
    detections && detectionsLive(detections, now)
      ? detections.frame.frame_id
      : null;
  return memory.tracks.filter((track) => track.frameId !== live);
}

/**
 * Stored objects drawn as 3D boxes: backend person records that detections
 * linked to a retained marker are represented by that marker (or were observed
 * empty), so their running-mean boxes are not drawn again. The selected record
 * is always drawn; Spatial memory and the inspector keep every record.
 */
export function displayedStoredObjects(
  objects: WorldObject[],
  memory: PersonMemory,
  selected: string | null,
): WorldObject[] {
  if (!memory.linkedObjectIds.length) return objects;
  const linked = new Set(memory.linkedObjectIds);
  return objects.filter(
    (o) => o.id === selected || o.class !== "person" || !linked.has(o.id),
  );
}
