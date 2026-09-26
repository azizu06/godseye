import type {
  ChangeEvent,
  Health,
  Message,
  Occupancy,
  PointChunk,
  Pose,
  Vec3,
  WorldObject,
} from "./protocol";
export interface Mission {
  health: Health | null;
  healthAt: number;
  pose: Pose | null;
  chunks: PointChunk[];
  occupancy: Occupancy | null;
  path: [number, number][];
  objects: WorldObject[];
  events: ChangeEvent[];
  trajectory: Vec3[];
  received: number;
}
export const emptyMission = (): Mission => ({
  health: null,
  healthAt: 0,
  pose: null,
  chunks: [],
  occupancy: null,
  path: [],
  objects: [],
  events: [],
  trajectory: [],
  received: 0,
});
export function reduceMessage(
  state: Mission,
  message: Message,
  now = Date.now(),
): Mission {
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
      if (state.chunks.some((c) => c.chunk_id === message.chunk_id))
        return next;
      const chunks = [...state.chunks, message].slice(-120);
      let count = chunks.reduce((n, c) => n + c.positions.length / 3, 0);
      while (count > 60000 && chunks.length > 1)
        count -= chunks.shift()!.positions.length / 3;
      return { ...next, chunks };
    }
    case "occupancy":
      return { ...next, occupancy: message };
    case "path":
      return { ...next, path: message.points };
    case "objects":
      return { ...next, objects: message.objects };
    case "event":
      return state.events.some(
        (e) =>
          e.t === message.t &&
          e.object_id === message.object_id &&
          e.kind === message.kind,
      )
        ? next
        : { ...next, events: [...state.events, message].slice(-200) };
  }
}
