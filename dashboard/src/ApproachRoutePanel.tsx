import { useCallback, useEffect, useState } from "react";
import { X } from "lucide-react";
import type { Vec2 } from "./protocol";
import type { Mission } from "./state";
import { objectEvidence, objectStale } from "./objectDisplay";
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
      automatic?: boolean;
    }
  | {
      phase: "planning" | "ready";
      objectId: string;
      mapKey: string | null;
      start: Vec2;
      result?: RouteResult;
      automatic?: boolean;
    };

/**
 * A suggested walking approach to a remembered person, from an operator-selected
 * start or recorded mission entry. Visualization only: it never sends a rover goal or motion. A new map,
 * new occupancy evidence or a moved person hides the route until it is rechecked.
 */
export function useApproachRoute(
  apiUrl: string,
  mission: Mission,
  { ready, sourceKey, now }: { ready: boolean; sourceKey: string; now: number },
) {
  const [route, setRoute] = useState<RouteState>({ phase: "idle" });
  const [selectedSource, setSelectedSource] = useState(sourceKey);
  const policyKey = JSON.stringify([sourceKey, mission.mapKey]);
  const [autoPolicy, setAutoPolicy] = useState({
    key: policyKey,
    dismissed: [] as string[],
    manual: false,
  });
  useEffect(
    () => setAutoPolicy({ key: policyKey, dismissed: [], manual: false }),
    [policyKey],
  );
  const policy =
    autoPolicy.key === policyKey
      ? autoPolicy
      : { key: policyKey, dismissed: [], manual: false };
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
  const people = mission.objects
    .filter(
      (o) =>
        o.class === "person" &&
        (o.state === "present" || o.state === "moved") &&
        !objectStale(o, now) &&
        objectEvidence(o) === "strong",
    )
    .sort((a, b) => a.first_seen - b.first_seen || a.id.localeCompare(b.id));
  const peopleKey = JSON.stringify(people.map((o) => o.id));
  const candidateEntry = mission.health?.mission_entry;
  const entry =
    candidateEntry &&
    JSON.stringify([candidateEntry.session_id, candidateEntry.map_epoch]) ===
      mission.mapKey
      ? candidateEntry
      : null;
  const entryKey = JSON.stringify(entry);
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
              : scopedRoute.phase !== "idle" &&
                  scopedRoute.automatic &&
                  objectEvidence(person) !== "strong"
                ? "person_unconfirmed"
                : scopedRoute.phase !== "idle" &&
                    scopedRoute.automatic &&
                    !entry
                  ? "mission_entry_unavailable"
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
    if (
      !ready ||
      mission.objects === interruptedObjects ||
      mission.health?.mode !== "explore" ||
      policy.manual
    )
      return;
    if (scopedRoute.phase === "idle") {
      const target = people.find((o) => !policy.dismissed.includes(o.id));
      if (!target) return;
      setSelectedSource(sourceKey);
      setRoute(
        entry
          ? {
              phase: "planning",
              objectId: target.id,
              mapKey: mission.mapKey,
              automatic: true,
              start: entry.start,
            }
          : {
              phase: "picking",
              objectId: target.id,
              mapKey: mission.mapKey,
              automatic: true,
            },
      );
    } else if (scopedRoute.automatic && !("start" in scopedRoute) && entry) {
      setRoute({ ...scopedRoute, phase: "planning", start: entry.start });
    }
  }, [
    ready,
    interruptedObjects,
    mission.objects,
    mission.health?.mode,
    policy,
    peopleKey,
    entryKey,
    scopedRoute.phase,
    scopedRoute.phase !== "idle" && scopedRoute.automatic,
    sourceKey,
    mission.mapKey,
  ]);
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
      setAutoPolicy((p) => ({ ...p, key: policyKey, manual: true }));
      setSelectedSource(sourceKey);
      setRoute({ phase: "picking", objectId: id, mapKey: mission.mapKey });
    },
    [mission.mapKey, sourceKey, policyKey],
  );
  const pickStart = useCallback(
    (x: number, z: number) =>
      setRoute((r) =>
        r.phase === "picking" && !r.automatic && !unavailable
          ? { ...r, phase: "planning", start: [x, z] }
          : r,
      ),
    [unavailable],
  );
  const clear = useCallback(() => {
    // A dismissal acknowledges the currently known people; repeated snapshots do not reopen it.
    if (scopedRoute.phase !== "idle" && scopedRoute.automatic)
      setAutoPolicy((p) => ({
        ...p,
        key: policyKey,
        dismissed: [
          ...new Set([
            ...p.dismissed,
            ...people.map((o) => o.id),
            scopedRoute.objectId,
          ]),
        ],
      }));
    setRoute({ phase: "idle" });
  }, [policyKey, peopleKey, scopedRoute]);
  const selectPerson = (id: string) => {
    if (
      scopedRoute.phase === "idle" ||
      !scopedRoute.automatic ||
      !people.some((o) => o.id === id)
    )
      return;
    setRoute(
      entry
        ? {
            phase: "planning",
            objectId: id,
            mapKey: mission.mapKey,
            automatic: true,
            start: entry.start,
          }
        : {
            phase: "picking",
            objectId: id,
            mapKey: mission.mapKey,
            automatic: true,
          },
    );
  };
  // Hide unsupported results during render, before effect cleanup aborts the request.
  const visibleRoute: RouteState =
    scopedRoute.phase !== "idle" && unavailable
      ? {
          ...scopedRoute,
          result: { status: "unavailable", reason: unavailable },
        }
      : scopedRoute;
  return {
    route: visibleRoute,
    person,
    begin,
    pickStart,
    clear,
    selectPerson,
    peopleCount: people.length,
    personFound:
      ready &&
      mission.objects !== interruptedObjects &&
      people.some((o) => o.id === person?.id),
  };
}

export function ApproachRouteCard({
  state,
  personLastSeen,
  now,
  onClear,
  personFound = false,
  peopleCount = 0,
}: {
  state: RouteState;
  personLastSeen: number | null;
  now: number;
  onClear: () => void;
  personFound?: boolean;
  peopleCount?: number;
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
        {state.automatic ? (
          <strong
            className="person-found"
            data-testid={personFound ? "person-found" : undefined}
            role="status"
          >
            {personFound ? "Person found" : "Person last seen"}
          </strong>
        ) : (
          <span className="eyebrow">
            Suggested approach · visualization only
          </span>
        )}
        <button
          className="icon-button"
          aria-label="Clear approach route"
          onClick={onClear}
        >
          <X size={14} />
        </button>
      </header>
      {state.automatic && (
        <p className="route-assumptions">
          {peopleCount > 1 ? `${peopleCount} people observed · ` : ""}
          {result?.status === "unavailable" &&
          result.reason === "mission_entry_unavailable"
            ? "Mission entry not recorded"
            : "From fixed mission entry"}
          {" · suggested approach"}
        </p>
      )}
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
