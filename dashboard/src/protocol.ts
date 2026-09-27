import { parseBinaryPoints } from "./pointCloud";
export type Vec3 = [number, number, number];
export type Vec2 = [number, number];
export type Mode = "manual" | "navigate" | "explore";
export type ObjectState =
  "present" | "last_seen" | "moved" | "not_found_on_rescan";
export interface WorldObject {
  id: string;
  class: string;
  position: Vec3;
  confidence: number;
  first_seen: number;
  last_seen: number;
  observations: number;
  state: ObjectState;
}
export interface MissionEntry {
  session_id: string;
  map_epoch: number;
  start: Vec2;
  frame_id: number;
  t_capture: number;
  started_at_ms: number;
  basis: "explore_start";
}
export interface Health {
  phone: "ok" | "stale" | "down";
  car: "ok" | "stale" | "down";
  detector: "ok" | "stale" | "down";
  pose_age_ms: number | null;
  mode: Mode;
  armed: boolean;
  stop_reason: string | null;
  navigation_wait_reason?: string | null;
  mission_entry?: MissionEntry | null;
}
export interface Pose {
  position: Vec3;
  yaw_rad: number;
  tracking: "normal" | "limited" | "not_available";
}
export interface PointChunk {
  chunk_id: number;
  positions: number[] | Float32Array;
  colors: number[] | Float32Array;
}
export interface Occupancy {
  origin: Vec2;
  cell_m: number;
  width: number;
  height: number;
  cells: string;
  /** Additive: estimated floor in world Y meters. Validated where it is used. */
  floor_y?: number | null;
}
export interface ChangeEvent {
  id?: string;
  rescan_id?: string;
  new_object_id?: string | null;
  kind: "new" | "moved" | "possible_move" | "not_found";
  object_id: string;
  old_position?: Vec3 | null;
  new_position?: Vec3 | null;
  displacement_m?: number | null;
  t: number;
}
/** One detector box in its frame's JPEG pixels; `position` only with same-frame depth. */
export interface DetectionBox {
  class: string;
  confidence: number;
  box: [number, number, number, number];
  position: Vec3 | null;
  depth_m: number | null;
  object_id: string | null;
}
export interface DetectionFrame {
  frame_id: number;
  t_capture: number;
  t_wall_ms: number;
  image: { width: number; height: number };
  source: "backend_detector";
  classes: string[];
  detections: DetectionBox[];
}
export interface MapScope {
  session_id?: string | null;
  map_epoch?: number | null;
}
type Wire<T extends string, P> = { version: 1; type: T } & MapScope & P;
export function mapKey(scope: MapScope): string | null | undefined {
  if (scope.session_id === undefined && scope.map_epoch === undefined)
    return undefined;
  if (scope.session_id === null && scope.map_epoch === null) return null;
  return JSON.stringify([scope.session_id, scope.map_epoch]);
}
export type Message =
  | Wire<"health", Health>
  | Wire<"pose", Pose>
  | Wire<"points", PointChunk>
  | Wire<"occupancy", Occupancy>
  | Wire<"path", { points: Vec2[] }>
  | Wire<"objects", { objects: WorldObject[] }>
  | Wire<"event", ChangeEvent>
  | Wire<"detections", DetectionFrame>;
export const isFiniteNumber = (n: unknown): n is number =>
  typeof n === "number" && Number.isFinite(n);
function validMissionEntry(value: unknown): value is MissionEntry {
  if (!value || typeof value !== "object") return false;
  const e = value as Record<string, unknown>;
  return (
    typeof e.session_id === "string" &&
    e.session_id.length > 0 &&
    e.session_id.length <= 256 &&
    Number.isSafeInteger(e.map_epoch) &&
    Number(e.map_epoch) >= 0 &&
    Array.isArray(e.start) &&
    e.start.length === 2 &&
    e.start.every(isFiniteNumber) &&
    Number.isSafeInteger(e.frame_id) &&
    Number(e.frame_id) >= 0 &&
    isFiniteNumber(e.t_capture) &&
    e.t_capture >= 0 &&
    Number.isSafeInteger(e.started_at_ms) &&
    Number(e.started_at_ms) >= 0 &&
    e.basis === "explore_start"
  );
}
const vector = (v: unknown, size: number) =>
  Array.isArray(v) && v.length === size && v.every(isFiniteNumber);
const member = (v: unknown, values: string[]) =>
  typeof v === "string" && values.includes(v);
export function decodeCells(grid: Occupancy): Uint8Array {
  return Uint8Array.from(atob(grid.cells), (c) => c.charCodeAt(0));
}
export function parseMessage(raw: unknown): Message | null {
  try {
    if (raw instanceof ArrayBuffer) {
      const chunk = parseBinaryPoints(raw);
      return chunk
        ? ({
            ...chunk,
            version: 1,
            positions: chunk.positions as Float32Array,
            colors: Float32Array.from(chunk.colors, (c) => c / 255),
          } as Message)
        : null;
    }
    if (typeof raw === "string") {
      if (raw.length > 8_000_000) return null;
      raw = JSON.parse(raw);
    }
    if (!raw || typeof raw !== "object") return null;
    const m = raw as Record<string, unknown>;
    if (m.version !== 1) return null;
    if (m.session_id !== undefined || m.map_epoch !== undefined) {
      const empty =
        m.session_id === null && m.map_epoch === null && m.type === "objects";
      if (
        !empty &&
        !(
          typeof m.session_id === "string" &&
          m.session_id.length > 0 &&
          m.session_id.length <= 256 &&
          Number.isSafeInteger(m.map_epoch) &&
          Number(m.map_epoch) >= 0
        )
      )
        return null;
    }
    let valid = false;
    switch (m.type) {
      case "health":
        valid =
          ["phone", "car", "detector"].every((k) =>
            member(m[k], ["ok", "stale", "down"]),
          ) &&
          (m.pose_age_ms === null ||
            (isFiniteNumber(m.pose_age_ms) && m.pose_age_ms >= 0)) &&
          member(m.mode, ["manual", "navigate", "explore"]) &&
          typeof m.armed === "boolean" &&
          (m.stop_reason === null || typeof m.stop_reason === "string") &&
          (m.navigation_wait_reason === undefined ||
            m.navigation_wait_reason === null ||
            typeof m.navigation_wait_reason === "string");
        break;
      case "pose":
        valid =
          vector(m.position, 3) &&
          isFiniteNumber(m.yaw_rad) &&
          member(m.tracking, ["normal", "limited", "not_available"]);
        break;
      case "points":
        valid =
          Number.isSafeInteger(m.chunk_id) &&
          Array.isArray(m.positions) &&
          Array.isArray(m.colors) &&
          m.positions.length <= 180000 &&
          m.positions.length % 3 === 0 &&
          m.positions.length === m.colors.length &&
          m.positions.every(isFiniteNumber) &&
          m.colors.every((c) => isFiniteNumber(c) && c >= 0 && c <= 1);
        break;
      case "occupancy": {
        if (
          !vector(m.origin, 2) ||
          !isFiniteNumber(m.cell_m) ||
          m.cell_m <= 0 ||
          !Number.isInteger(m.width) ||
          !Number.isInteger(m.height) ||
          typeof m.cells !== "string"
        )
          break;
        const n = Number(m.width) * Number(m.height);
        if (
          Number(m.width) <= 0 ||
          Number(m.height) <= 0 ||
          n > 262144 ||
          m.cells.length > 400000
        )
          break;
        const cells = atob(m.cells);
        valid =
          cells.length === n && [...cells].every((c) => c.charCodeAt(0) <= 2);
        break;
      }
      case "path":
        valid =
          Array.isArray(m.points) &&
          m.points.length <= 10000 &&
          m.points.every((p) => vector(p, 2));
        break;
      case "objects":
        valid =
          Array.isArray(m.objects) &&
          m.objects.length <= 2000 &&
          m.objects.every(
            (o) =>
              o &&
              typeof o.id === "string" &&
              o.id.length <= 256 &&
              typeof o.class === "string" &&
              o.class.length <= 128 &&
              vector(o.position, 3) &&
              isFiniteNumber(o.confidence) &&
              o.confidence >= 0 &&
              o.confidence <= 1 &&
              isFiniteNumber(o.first_seen) &&
              isFiniteNumber(o.last_seen) &&
              Number.isSafeInteger(o.observations) &&
              o.observations >= 0 &&
              member(o.state, [
                "present",
                "last_seen",
                "moved",
                "not_found_on_rescan",
              ]),
          );
        break;
      case "detections": {
        const image = m.image as Record<string, unknown> | null;
        if (
          typeof m.session_id !== "string" ||
          m.source !== "backend_detector" ||
          !Number.isSafeInteger(m.frame_id) ||
          !isFiniteNumber(m.t_capture) ||
          !isFiniteNumber(m.t_wall_ms) ||
          !image ||
          typeof image !== "object" ||
          !Number.isSafeInteger(image.width) ||
          !Number.isSafeInteger(image.height) ||
          Number(image.width) <= 0 ||
          Number(image.height) <= 0 ||
          !Array.isArray(m.classes) ||
          m.classes.length > 64 ||
          !m.classes.every((c) => typeof c === "string" && c.length <= 128) ||
          !Array.isArray(m.detections) ||
          m.detections.length > 64
        )
          break;
        const width = Number(image.width),
          height = Number(image.height);
        valid = m.detections.every((d) => {
          if (!d || typeof d !== "object") return false;
          const [x1, y1, x2, y2] = vector(d.box, 4) ? d.box : [];
          return (
            typeof d.class === "string" &&
            d.class.length <= 128 &&
            isFiniteNumber(d.confidence) &&
            d.confidence >= 0 &&
            d.confidence <= 1 &&
            x1 !== undefined &&
            0 <= x1 &&
            x1 < x2 &&
            x2 <= width &&
            0 <= y1 &&
            y1 < y2 &&
            y2 <= height &&
            (d.position === null || vector(d.position, 3)) &&
            (d.depth_m === null ||
              (isFiniteNumber(d.depth_m) && d.depth_m > 0)) &&
            (d.object_id === null ||
              (typeof d.object_id === "string" && d.object_id.length <= 256))
          );
        });
        break;
      }
      case "event":
        valid =
          [m.id, m.rescan_id].every(
            (id) =>
              id === undefined ||
              (typeof id === "string" && id.length > 0 && id.length <= 256),
          ) &&
          (m.new_object_id == null ||
            (typeof m.new_object_id === "string" &&
              m.new_object_id.length <= 256)) &&
          member(m.kind, ["new", "moved", "possible_move", "not_found"]) &&
          typeof m.object_id === "string" &&
          isFiniteNumber(m.t) &&
          [m.old_position, m.new_position].every(
            (v) => v == null || vector(v, 3),
          ) &&
          (m.displacement_m == null ||
            (isFiniteNumber(m.displacement_m) && m.displacement_m >= 0));
        break;
    }
    if (valid && m.type === "health" && "mission_entry" in m)
      return {
        ...m,
        mission_entry: validMissionEntry(m.mission_entry)
          ? m.mission_entry
          : null,
      } as Message;
    return valid ? (raw as Message) : null;
  } catch {
    return null;
  }
}

export function parseEventHistory(
  raw: unknown,
  expectedKey: string,
): Message[] | null {
  if (!raw || typeof raw !== "object") return null;
  const data = raw as Record<string, unknown>;
  if (
    data.version !== 1 ||
    !Array.isArray(data.events) ||
    data.events.length > 2000
  )
    return null;
  const envelope = parseMessage({ ...data, type: "objects", objects: [] });
  if (!envelope || mapKey(envelope) !== expectedKey) return null;
  const events = data.events.map((event) =>
    parseMessage({
      ...event,
      version: 1,
      type: "event",
      session_id: envelope.session_id,
      map_epoch: envelope.map_epoch,
    }),
  );
  return events.every((event): event is Message => event !== null)
    ? events
    : null;
}
