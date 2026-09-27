import { isFiniteNumber, type Vec2 } from "./protocol";

/** The backend's stated demo assumptions; a suggested route is never verified. */
export interface RouteAssumptions {
  walker_radius_m: number;
  margin_m: number;
  person_keep_out_m: number;
  approach_reach_m: number;
  unknown: "blocked";
  doors: "not_inferred";
  verified: false;
}
export type RouteResult =
  | {
      status: "ok";
      points: Vec2[];
      approach: Vec2;
      length_m: number;
      assumptions: RouteAssumptions;
    }
  | { status: "unavailable"; reason: string; assumptions?: RouteAssumptions };

export const reasonText: Record<string, string> = {
  no_observed_map: "no observed floor map yet",
  start_or_person_off_map: "start or person is outside the observed map",
  start_not_observed_free:
    "the selected start is not on observed-free floor with walker clearance",
  no_observed_free_approach:
    "no observed-free spot with walker clearance near the person",
  no_observed_free_route:
    "no observed-free connection with walker clearance (unknown space is not assumed passable)",
  person_not_in_map: "the person is no longer in the active map",
  map_changed: "the map changed while planning",
  route_service_unavailable: "the backend route service is unavailable",
};

const pair = (v: unknown): v is Vec2 =>
  Array.isArray(v) && v.length === 2 && v.every(isFiniteNumber);

/** Accept only a response for exactly this map, person and start. */
export function parseRoute(
  raw: unknown,
  expect: {
    session_id: string;
    map_epoch: number;
    object_id: string;
    start: Vec2;
  },
): RouteResult | null {
  if (!raw || typeof raw !== "object") return null;
  const m = raw as Record<string, unknown>;
  if (
    m.version !== 1 ||
    m.session_id !== expect.session_id ||
    m.map_epoch !== expect.map_epoch ||
    m.object_id !== expect.object_id ||
    !pair(m.start) ||
    m.start[0] !== expect.start[0] ||
    m.start[1] !== expect.start[1]
  )
    return null;
  const a = m.assumptions as Record<string, unknown> | undefined;
  if (!a || a.verified !== false || a.unknown !== "blocked") return null;
  if (m.status === "unavailable" && typeof m.reason === "string")
    return {
      status: "unavailable",
      reason: m.reason,
      assumptions: a as unknown as RouteAssumptions,
    };
  if (
    m.status === "ok" &&
    Array.isArray(m.points) &&
    m.points.length >= 2 &&
    m.points.length <= 4000 &&
    m.points.every(pair) &&
    pair(m.approach) &&
    isFiniteNumber(m.length_m)
  )
    return {
      status: "ok",
      points: m.points as Vec2[],
      approach: m.approach,
      length_m: m.length_m,
      assumptions: a as unknown as RouteAssumptions,
    };
  return null;
}

export async function requestRoute(
  apiUrl: string,
  body: {
    session_id: string;
    map_epoch: number;
    object_id: string;
    start: Vec2;
  },
  signal: AbortSignal,
): Promise<RouteResult> {
  const response = await fetch(`${apiUrl.replace(/\/$/, "")}/route`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (response.status === 404)
    return { status: "unavailable", reason: "person_not_in_map" };
  if (response.status === 409)
    return { status: "unavailable", reason: "map_changed" };
  const parsed = response.ok ? parseRoute(await response.json(), body) : null;
  return (
    parsed ?? { status: "unavailable", reason: "route_service_unavailable" }
  );
}

export function mapScope(mapKey: string | null) {
  if (!mapKey) return null;
  const [session_id, map_epoch] = JSON.parse(mapKey) as [unknown, unknown];
  return typeof session_id === "string" && Number.isSafeInteger(map_epoch)
    ? { session_id, map_epoch: Number(map_epoch) }
    : null;
}
