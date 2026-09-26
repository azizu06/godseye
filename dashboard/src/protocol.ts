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
export interface Health {
  phone: "ok" | "stale" | "down";
  car: "ok" | "stale" | "down";
  detector: "ok" | "stale" | "down";
  pose_age_ms: number | null;
  mode: Mode;
  armed: boolean;
  stop_reason: string | null;
}
export interface Pose {
  position: Vec3;
  yaw_rad: number;
  tracking: "normal" | "limited" | "not_available";
}
export interface PointChunk {
  chunk_id: number;
  positions: number[];
  colors: number[];
}
export interface Occupancy {
  origin: Vec2;
  cell_m: number;
  width: number;
  height: number;
  cells: string;
}
export interface ChangeEvent {
  kind: "new" | "moved" | "possible_move" | "not_found";
  object_id: string;
  old_position?: Vec3 | null;
  new_position?: Vec3 | null;
  displacement_m?: number | null;
  t: number;
}
type Wire<T extends string, P> = { version: 1; type: T } & P;
export type Message =
  | Wire<"health", Health>
  | Wire<"pose", Pose>
  | Wire<"points", PointChunk>
  | Wire<"occupancy", Occupancy>
  | Wire<"path", { points: Vec2[] }>
  | Wire<"objects", { objects: WorldObject[] }>
  | Wire<"event", ChangeEvent>;
export const isFiniteNumber = (n: unknown): n is number =>
  typeof n === "number" && Number.isFinite(n);
const vector = (v: unknown, size: number) =>
  Array.isArray(v) && v.length === size && v.every(isFiniteNumber);
const member = (v: unknown, values: string[]) =>
  typeof v === "string" && values.includes(v);
export function decodeCells(grid: Occupancy): Uint8Array {
  return Uint8Array.from(atob(grid.cells), (c) => c.charCodeAt(0));
}
export function parseMessage(raw: unknown): Message | null {
  try {
    if (typeof raw === "string") {
      if (raw.length > 8_000_000) return null;
      raw = JSON.parse(raw);
    }
    if (!raw || typeof raw !== "object") return null;
    const m = raw as Record<string, unknown>;
    if (m.version !== 1) return null;
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
          (m.stop_reason === null || typeof m.stop_reason === "string");
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
      case "event":
        valid =
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
    return valid ? (raw as Message) : null;
  } catch {
    return null;
  }
}
