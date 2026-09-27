import { useEffect, useMemo, useRef, useState } from "react";
import type { Mission } from "./state";
import { objectEvidence, objectStale } from "./objectDisplay";
import { mapScope, type RouteResult } from "./approachRoute";
import { ReconPlanner, type ReconResult } from "./reconPlanner";
import type { ApproachDrawing } from "./SceneLabels";

export function useReconApproaches(
  apiUrl: string,
  mission: Mission,
  { ready, sourceKey, now }: { ready: boolean; sourceKey: string; now: number },
) {
  const scopeKey = JSON.stringify([sourceKey, mission.mapKey]);
  const [admitted, setAdmitted] = useState<{ scope: string; ids: string[] }>({
    scope: scopeKey,
    ids: [],
  });
  const [results, setResults] = useState<Record<string, ReconResult>>({});
  const planner = useRef<ReconPlanner | null>(null);
  const [interrupted, setInterrupted] = useState<Mission["objects"] | null>(
    () => (ready ? null : mission.objects),
  );
  useEffect(() => {
    if (!ready) setInterrupted(mission.objects);
  }, [ready]);
  const observed = ready && mission.objects !== interrupted;
  const eligible = mission.objects.filter(
    (o) =>
      o.class === "person" &&
      objectEvidence(o) === "strong" &&
      (o.state === "present" || o.state === "moved") &&
      !objectStale(o, now),
  );
  const eligibleIds = eligible.map((o) => o.id).sort();
  useEffect(() => {
    setAdmitted((previous) => {
      const ids = previous.scope === scopeKey ? previous.ids : [];
      const next =
        observed && mission.health?.mode === "explore"
          ? [...new Set([...ids, ...eligibleIds])]
          : ids;
      return previous.scope === scopeKey && next.length === previous.ids.length
        ? previous
        : { scope: scopeKey, ids: next };
    });
  }, [scopeKey, observed, mission.health?.mode, JSON.stringify(eligibleIds)]);
  useEffect(() => {
    const current = new ReconPlanner((id, result) =>
      setResults((previous) => ({ ...previous, [id]: result })),
    );
    planner.current = current;
    setResults({});
    return () => {
      current.dispose();
      planner.current = null;
    };
  }, [scopeKey]);
  // Equal occupancy snapshots must not continuously restart the queue.
  const occupancy = useMemo(
    () => JSON.stringify(mission.occupancy),
    [mission.occupancy],
  );
  const evidence = useRef({ encoded: occupancy, revision: 0 });
  if (evidence.current.encoded !== occupancy)
    evidence.current = {
      encoded: occupancy,
      revision: evidence.current.revision + 1,
    };
  const candidate = mission.health?.mission_entry;
  const entry =
    candidate &&
    JSON.stringify([candidate.session_id, candidate.map_epoch]) ===
      mission.mapKey
      ? candidate
      : null;
  const scope = mapScope(mission.mapKey);
  const ids = admitted.scope === scopeKey ? admitted.ids : [];
  const versions = useRef({
    scope: scopeKey,
    values: new Map<string, { signature: string; version: number }>(),
  });
  if (versions.current.scope !== scopeKey)
    versions.current = { scope: scopeKey, values: new Map() };
  const rows = ids.map((id, index) => {
    const person = mission.objects.find(
      (o) => o.id === id && o.class === "person",
    );
    const reason = !ready
      ? "route_feed_unavailable"
      : !person
        ? "person_not_in_map"
        : person.state === "not_found_on_rescan"
          ? "person_not_found"
          : person.state === "last_seen" || objectEvidence(person) !== "strong"
            ? "person_unconfirmed"
            : objectStale(person, now)
              ? "person_stale"
              : !observed
                ? "person_unconfirmed"
                : !entry
                  ? "mission_entry_unavailable"
                  : null;
    const signature = JSON.stringify([
      scopeKey,
      entry?.start,
      person?.position,
      evidence.current.revision,
      reason,
    ]);
    const previous = versions.current.values.get(id);
    const version =
      previous?.signature === signature
        ? previous.version
        : (previous?.version ?? 0) + 1;
    versions.current.values.set(id, { signature, version });
    const key = JSON.stringify([scopeKey, id, version]);
    const result: RouteResult | undefined = reason
      ? { status: "unavailable", reason }
      : results[id]?.key === key
        ? results[id].result
        : undefined;
    const job =
      !reason && entry && scope
        ? {
            id,
            key,
            apiUrl,
            body: {
              ...scope,
              object_id: id,
              start: entry.start,
              purpose: "recon" as const,
            },
          }
        : null;
    return {
      id,
      number: index + 1,
      person,
      result,
      job,
      fresh: observed && eligibleIds.includes(id),
    };
  });
  const jobs = rows.flatMap((row) => (row.job ? [row.job] : []));
  const jobsKey = JSON.stringify(jobs.map((job) => [job.id, job.key]));
  useEffect(() => {
    planner.current?.reconcile(jobs);
  }, [scopeKey, jobsKey]);
  const drawings: ApproachDrawing[] = entry
    ? rows.flatMap((row) =>
        row.result?.status === "ok"
          ? [
              {
                id: row.id,
                label: `PERSON ${row.number}`,
                start: entry.start,
                startKind: "mission" as const,
                points: row.result.points,
                approach: row.result.approach,
              },
            ]
          : [],
      )
    : [];
  return { rows, drawings, freshCount: rows.filter((row) => row.fresh).length };
}
