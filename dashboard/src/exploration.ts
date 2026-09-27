import type { Health } from "./protocol";

const phases = [
  "idle",
  "selecting",
  "moving",
  "aligning",
  "settling",
  "scanning",
  "complete",
  "blocked",
] as const;
export interface ExplorationStatus {
  phase: (typeof phases)[number];
  reason: string | null;
  session_id: string | null;
  map_epoch: number | null;
  observed_views: number;
  gained_cells: number;
  last_gain_cells: number | null;
  gained_surface_voxels: number;
  last_gain_surface_voxels: number | null;
  target: [number, number] | null;
}
const count = (value: unknown): value is number =>
  Number.isSafeInteger(value) && Number(value) >= 0;
export function parseExploration(raw: unknown): ExplorationStatus | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const scope =
    (value.session_id === null && value.map_epoch === null) ||
    (typeof value.session_id === "string" &&
      value.session_id.length > 0 &&
      value.session_id.length <= 256 &&
      count(value.map_epoch));
  if (
    !phases.includes(value.phase as ExplorationStatus["phase"]) ||
    !scope ||
    !(
      value.reason === null ||
      (typeof value.reason === "string" && value.reason.length <= 256)
    ) ||
    ![
      value.observed_views,
      value.gained_cells,
      value.gained_surface_voxels,
    ].every(count) ||
    ![value.last_gain_cells, value.last_gain_surface_voxels].every(
      (v) => v === null || count(v),
    ) ||
    !(
      value.target === null ||
      (Array.isArray(value.target) &&
        value.target.length === 2 &&
        value.target.every((v) => typeof v === "number" && Number.isFinite(v)))
    )
  )
    return null;
  return { ...value } as unknown as ExplorationStatus;
}

const reasons: Record<string, string> = {
  sensing_clearance_unknown: "Floor clearance is still unknown.",
  sensing_stale: "Waiting for fresh depth observations.",
  scan_capture_unusable: "The phone view cannot support a scan here.",
  scan_view_unsupported:
    "This viewpoint cannot provide reliable scan evidence.",
  scan_timeout: "Scanning timed out at this viewpoint.",
  scan_budget: "The scan reached its configured limit.",
  search_limit: "Planning reached its search limit.",
  scan_search_limit: "Scanning reached its search limit.",
  operator_stop: "Stopped by the operator.",
  phone_disconnected: "The phone disconnected.",
  tracking_lost: "Phone tracking was lost.",
};
const reasonText = (reason: string | null) =>
  reason
    ? (reasons[reason] ?? reason.replaceAll("_", " "))
    : "The backend stopped this scan.";
export interface ExplorationPresentation {
  title: string;
  detail: string;
  progress?: string;
  tone: "neutral" | "active" | "warning";
}
/** Display only the backend's scoped evidence; never infer coverage or motor state. */
export function explorationPresentation(
  health: Health | null,
  stale: boolean,
  activeMap: string | null,
): ExplorationPresentation | null {
  if (!health || health.mode !== "explore") return null;
  if (stale)
    return {
      title: "Explore status stale",
      detail:
        "Waiting for fresh backend health. Movement status is unverified.",
      tone: "warning",
    };
  const scan = health.exploration;
  if (!scan)
    return {
      title: "Explore status unavailable",
      detail: "This backend has not supplied scan progress.",
      tone: "neutral",
    };
  const key =
    scan.session_id === null
      ? null
      : JSON.stringify([scan.session_id, scan.map_epoch]);
  if (key !== activeMap || (key === null && scan.phase !== "idle"))
    return {
      title: "Waiting for scan status",
      detail: "Waiting for scan evidence from the current map.",
      tone: "neutral",
    };
  const progress = `${scan.observed_views.toLocaleString()} stable views · ${scan.gained_surface_voxels.toLocaleString()} new surface voxels`;
  if (scan.phase === "complete")
    return scan.reason === "scan_accessible_exhausted"
      ? {
          title: "Accessible scan done",
          detail: "Accessible viewpoints exhausted. Unseen areas may remain.",
          progress,
          tone: "neutral",
        }
      : {
          title: "Scan status unavailable",
          detail: reasonText(scan.reason),
          tone: "warning",
        };
  if (scan.phase === "blocked")
    return {
      title: "Partial scan · blocked",
      detail: reasonText(scan.reason),
      progress,
      tone: "warning",
    };
  if (scan.phase !== "idle" && !health.armed)
    return {
      title: "Explore stopped",
      detail: reasonText(health.stop_reason),
      progress,
      tone: "neutral",
    };
  const descriptions = {
    idle: "No scan is running. Arming remains an explicit operator action.",
    selecting: "Choosing the next reachable viewpoint.",
    moving: "Following the planned route to the next viewpoint.",
    aligning: "Turning toward the planned scan view.",
    settling: "Waiting for the phone to settle before measuring coverage.",
    scanning: "Collecting stable depth observations at this viewpoint.",
  };
  return {
    title: `Explore · ${scan.phase}`,
    detail: descriptions[scan.phase],
    progress,
    tone: scan.phase === "idle" ? "neutral" : "active",
  };
}
