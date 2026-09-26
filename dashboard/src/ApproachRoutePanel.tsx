import { useCallback, useEffect, useState } from "react";
import { X } from "lucide-react";
import type { Vec2 } from "./protocol";
import type { Mission } from "./state";
import {
  mapScope,
  reasonText,
  requestRoute,
  type RouteResult,
} from "./approachRoute";

export type RouteState =
  | { phase: "idle" }
  | { phase: "picking"; objectId: string; mapKey: string | null }
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
export function useApproachRoute(apiUrl: string, mission: Mission) {
  const [route, setRoute] = useState<RouteState>({ phase: "idle" });
  const person =
    route.phase === "idle"
      ? undefined
      : mission.objects.find(
          (o) => o.id === route.objectId && o.class === "person",
        );
  const personKey = person ? JSON.stringify(person.position) : null;
  const start = "start" in route ? route.start : null;
  const startKey = start ? start.join(",") : null;
  const objectId = route.phase === "idle" ? null : route.objectId;
  useEffect(() => {
    setRoute((r) =>
      r.phase !== "idle" && r.mapKey !== mission.mapKey ? { phase: "idle" } : r,
    );
  }, [mission.mapKey]);
  useEffect(() => {
    if (!start || !objectId) return;
    const scope = mapScope(mission.mapKey);
    if (!person || !scope) {
      setRoute((r) =>
        "start" in r
          ? {
              ...r,
              phase: "ready",
              result: { status: "unavailable", reason: "person_not_in_map" },
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
    mission.mapKey,
    mission.occupancy,
    objectId,
    personKey,
    startKey,
  ]);
  const begin = useCallback(
    (id: string) =>
      setRoute({ phase: "picking", objectId: id, mapKey: mission.mapKey }),
    [mission.mapKey],
  );
  const pickStart = useCallback(
    (x: number, z: number) =>
      setRoute((r) =>
        r.phase === "picking" ? { ...r, phase: "planning", start: [x, z] } : r,
      ),
    [],
  );
  const clear = useCallback(() => setRoute({ phase: "idle" }), []);
  return { route, person, begin, pickStart, clear };
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
      {state.phase === "picking" && (
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
