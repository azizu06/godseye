import { mapKey } from "./protocol";
import {
  clearPeople,
  emptyPeople,
  observePeople,
  type PersonClearance,
  type PersonMemory,
} from "./personMemory";
import type {
  ChangeEvent,
  DetectionFrame,
  MapScope,
  Health,
  Message,
  Occupancy,
  PointChunk,
  Pose,
  Vec3,
  WorldObject,
} from "./protocol";
/** The newest received detector output and when this viewer received it. */
export interface ReceivedDetections {
  frame: DetectionFrame & MapScope;
  receivedAt: number;
}
export interface Mission {
  mapKey: string | null;
  health: Health | null;
  healthAt: number;
  pose: Pose | null;
  chunks: PointChunk[];
  pointIds: number[];
  occupancy: Occupancy | null;
  path: [number, number][];
  objects: WorldObject[];
  events: ChangeEvent[];
  detections: ReceivedDetections | null;
  /** Display-only retained people; reset with the map, kept across reconnects. */
  people: PersonMemory;
  trajectory: Vec3[];
  received: number;
}
export const emptyMission = (): Mission => ({
  mapKey: null,
  health: null,
  healthAt: 0,
  pose: null,
  chunks: [],
  pointIds: [],
  occupancy: null,
  path: [],
  objects: [],
  events: [],
  detections: null,
  people: emptyPeople(),
  trajectory: [],
  received: 0,
});
/**
 * A transport reconnect is not a new AR map. Keep historical spatial memory
 * (objects, occupancy and its floor, retained people), but require fresh
 * identity/pose/health before resuming live use. Point IDs may restart: clear
 * only deduplication, retaining observed geometry.
 */
export const reconnectMission = (state: Mission): Mission => ({
  ...state,
  health: null,
  healthAt: 0,
  pose: null,
  path: [],
  pointIds: [],
  detections: null,
});
/** Apply a depth proof only to the map it was measured in. */
export const clearMissionPeople = (
  state: Mission,
  map: string,
  cleared: readonly PersonClearance[],
): Mission =>
  state.mapKey === map
    ? { ...state, people: clearPeople(state.people, cleared) }
    : state;
export function reduceMessage(
  state: Mission,
  message: Message,
  now = Date.now(),
): Mission {
  const key = mapKey(message);
  if (key !== undefined && key !== state.mapKey)
    state = {
      ...emptyMission(),
      mapKey: key,
      health: state.health,
      healthAt: state.healthAt,
      received: state.received,
    };
  const next = { ...state, received: state.received + 1 };
  switch (message.type) {
    case "health":
      return { ...next, health: message, healthAt: now };
    case "pose": {
      const last = state.trajectory.at(-1);
      const p = message.position;
      const changed =
        !last || Math.hypot(last[0] - p[0], last[2] - p[2]) > 0.025;
      return {
        ...next,
        pose: message,
        trajectory: changed
          ? [...state.trajectory, p].slice(-600)
          : state.trajectory,
      };
    }
    case "points": {
      if (state.pointIds.includes(message.chunk_id)) return next;
      const chunks = [...state.chunks, message].slice(-4000);
      let count = chunks.reduce((n, c) => n + c.positions.length / 3, 0);
      while (count > 2000000 && chunks.length > 1)
        count -= chunks.shift()!.positions.length / 3;
      return {
        ...next,
        chunks,
        pointIds: [...state.pointIds, message.chunk_id].slice(-4000),
      };
    }
    case "occupancy":
      return { ...next, occupancy: message };
    case "path":
      return { ...next, path: message.points };
    case "objects":
      return { ...next, objects: message.objects };
    case "detections":
      return {
        ...next,
        detections: { frame: message, receivedAt: now },
        people: observePeople(state.people, message, now),
      };
    case "event":
      return state.events.some((e) =>
        message.id !== undefined && e.id !== undefined
          ? e.id === message.id
          : e.t === message.t &&
            e.object_id === message.object_id &&
            e.kind === message.kind,
      )
        ? next
        : {
            ...next,
            events: [...state.events, message]
              .sort(
                (a, b) => a.t - b.t || (a.id ?? "").localeCompare(b.id ?? ""),
              )
              .slice(-200),
          };
  }
}
