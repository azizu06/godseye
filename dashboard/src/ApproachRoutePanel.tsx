import { useCallback, useEffect, useState } from "react";
import { X } from "lucide-react";
import type { Vec2 } from "./protocol";
import type { Mission } from "./state";
import { objectStale } from "./objectDisplay";
import {
  mapScope,
  reasonText,
  requestRoute,
  type RouteResult,
} from "./approachRoute";

export type RouteState =
  | { phase: "idle" }
  | {
      phase: "picking";
      objectId: string;
      mapKey: string | null;
      result?: RouteResult;
    }
  | {
      phase: "planning" | "ready";
      objectId: string;
      mapKey: string | null;
      start: Vec2;
      result?: RouteResult;
    };

/**
 * A suggested walking approach to a remembered person, from an operator-selected
 * start. Visualization only: it never sends a rover goal or motion. A new map,
 * new occupancy evidence or a moved person hides the route until it is rechecked.
 */
export function useApproachRoute(
  apiUrl: string,
  mission: Mission,
  { ready, sourceKey, now }: { ready: boolean; sourceKey: string; now: number },
) {
  const [route, setRoute] = useState<RouteState>({ phase: "idle" });
  const [selectedSource, setSelectedSource] = useState(sourceKey);
  const [interruptedObjects, setInterruptedObjects] = useState<
    Mission["objects"] | null
  >(() => (ready ? null : mission.objects));
  useEffect(() => {
    // Remember the snapshot at loss of readiness, not snapshots arriving afterward.
    // Reconnection health/occupancy alone cannot revive a route to retained history.
    if (!ready) setInterruptedObjects(mission.objects);
  }, [ready]);
  const scopedRoute: RouteState =
    route.phase !== "idle" &&
    (route.mapKey !== mission.mapKey || selectedSource !== sourceKey)
      ? { phase: "idle" }
      : route;
  const person =
    scopedRoute.phase === "idle"
      ? undefined
      : mission.objects.find(
          (o) => o.id === scopedRoute.objectId && o.class === "person",
        );
  const unavailable = !ready
    ? "route_feed_unavailable"
    : !person
      ? "person_not_in_map"
      : person.state === "not_found_on_rescan"
        ? "person_not_found"
        : person.state === "last_seen"
          ? "person_unconfirmed"
          : objectStale(person, now)
            ? "person_stale"
            : mission.objects === interruptedObjects
              ? "person_unconfirmed"
              : null;
  const personKey = person ? JSON.stringify(person.position) : null;
  const start = "start" in scopedRoute ? scopedRoute.start : null;
  const startKey = start ? start.join(",") : null;
  const objectId = scopedRoute.phase === "idle" ? null : scopedRoute.objectId;
  useEffect(() => {
    setRoute((r) =>
      r.phase !== "idle" &&
      (r.mapKey !== mission.mapKey || selectedSource !== sourceKey)
        ? { phase: "idle" }
        : r,
    );
  }, [mission.mapKey, selectedSource, sourceKey]);
  useEffect(() => {
    if (!start || !objectId) return;
    const scope = mapScope(mission.mapKey);
    if (unavailable || !scope) {
      setRoute((r) =>
        "start" in r
          ? {
              ...r,
              phase: "ready",
              result: {
                status: "unavailable",
                reason: unavailable ?? "person_not_in_map",
              },
            }
          : r,
      );
      return;
    }
    const abort = new AbortController();
    // Changed blocking evidence retires the drawn route until it is rechecked.
    setRoute((r) =>
      "start" in r ? { ...r, phase: "planning", result: undefined } : r,
    );
    requestRoute(apiUrl, { ...scope, object_id: objectId, start }, abort.signal)
      .catch((): RouteResult => ({
        status: "unavailable",
        reason: "route_service_unavailable",
      }))
      .then((result) => {
        if (abort.signal.aborted) return;
        setRoute((r) =>
          "start" in r && r.objectId === objectId
            ? { ...r, phase: "ready", result }
            : r,
        );
      });
    return () => abort.abort();
    // Replan for new occupancy evidence, a moved person, map or start.
  }, [
    apiUrl,
    sourceKey,
    mission.mapKey,
    mission.occupancy,
    objectId,
    personKey,
    startKey,
    unavailable,
  ]);
  const begin = useCallback(
    (id: string) => {
      setSelectedSource(sourceKey);
      setRoute({ phase: "picking", objectId: id, mapKey: mission.mapKey });
    },
    [mission.mapKey, sourceKey],
  );
  const pickStart = useCallback(
    (x: number, z: number) =>
      setRoute((r) =>
        r.phase === "picking" && !unavailable
          ? { ...r, phase: "planning", start: [x, z] }
          : r,
      ),
    [unavailable],
  );
  const clear = useCallback(() => setRoute({ phase: "idle" }), []);
  // Hide unsupported results during render, before effect cleanup aborts the request.
  const visibleRoute: RouteState =
    scopedRoute.phase !== "idle" && unavailable
      ? {
          ...scopedRoute,
          result: { status: "unavailable", reason: unavailable },
        }
      : scopedRoute;
  return { route: visibleRoute, person, begin, pickStart, clear };
}

export function ApproachRouteCard({
  state,
  personLastSeen,
  now,
  onClear,
}: {
  state: RouteState;
  personLastSeen: number | null;
  now: number;
  onClear: () => void;
}) {
  if (state.phase === "idle") return null;
  const result = "result" in state ? state.result : undefined;
  const a = result?.assumptions;
  const seen =
    personLastSeen === null
      ? "person not in map"
      : `person last seen ${Math.max(0, Math.round(now / 1000 - personLastSeen))} s ago`;
  return (
    <section
      className="route-card"
      aria-label="Suggested approach route"
      data-testid="route-card"
    >
      <header>
        <span className="eyebrow">Suggested approach · visualization only</span>
        <button
          className="icon-button"
          aria-label="Clear approach route"
          onClick={onClear}
        >
          <X size={14} />
        </button>
      </header>
      {state.phase === "picking" && !result && (
        <p>
          Click the entrance or start point on the floor. The route begins
          exactly where you click.
        </p>
      )}
      {state.phase === "planning" && (
        <p>Checking observed-free floor against the current map…</p>
      )}
      {result?.status === "ok" && (
        <p data-testid="route-status">
          Route {result.length_m.toFixed(1)} m to an observed-free approach
          point beside the person · unverified · not a rover path
        </p>
      )}
      {result?.status === "unavailable" && (
        <p data-testid="route-status">
          Route unavailable · {reasonText[result.reason] ?? result.reason}
        </p>
      )}
      <p className="route-assumptions">
        {seen}
        {a &&
          ` · assumes a ${(a.walker_radius_m * 2).toFixed(1)} m wide walker with ${a.margin_m.toFixed(2)} m margin · observed-free cells only · unknown space blocked · doors not inferred`}
      </p>
    </section>
  );
}
