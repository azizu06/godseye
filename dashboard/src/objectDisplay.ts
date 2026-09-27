import type { WorldObject } from "./protocol";

/**
 * Default 3D display policy for stored objects. Display only: the backend keeps
 * every observation, and Spatial memory, the inspector and voice grounding are
 * unchanged. Hiding a weak detection does not make it wrong, and showing a
 * strong one does not make it right.
 */
export const DISPLAY_MIN_OBSERVATIONS = 2;
export const DISPLAY_MIN_CONFIDENCE = 0.5;
/** A stored object not re-observed for this long is labelled with its age, never as present. */
export const OBJECT_STALE_S = 30;

export type Evidence = "strong" | "weak" | "possible_person";

/**
 * `strong`: seen in at least two frames with confidence of at least 0.5.
 * `possible_person`: a weaker `person`; still drawn so a real person seen once
 * stays discoverable, but marked as uncertain.
 * `weak`: anything else; hidden from the 3D view by default.
 */
export function objectEvidence(o: WorldObject): Evidence {
  if (
    o.observations >= DISPLAY_MIN_OBSERVATIONS &&
    o.confidence >= DISPLAY_MIN_CONFIDENCE
  )
    return "strong";
  return o.class === "person" ? "possible_person" : "weak";
}

/** Objects drawn in 3D. The selected object is always drawn. */
export function displayedObjects(
  objects: WorldObject[],
  showWeak: boolean,
  selected: string | null,
): WorldObject[] {
  if (showWeak) return objects;
  return objects.filter(
    (o) => o.id === selected || objectEvidence(o) !== "weak",
  );
}

export const objectAgeS = (o: WorldObject, nowMs: number) =>
  Math.max(0, nowMs / 1000 - o.last_seen);

export const objectStale = (o: WorldObject, nowMs: number) =>
  objectAgeS(o, nowMs) > OBJECT_STALE_S;

export function ageLabel(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

/**
 * State wording for a stored object. A `present` object that has not been
 * re-observed recently reads as last seen, so a disconnected or finished scan
 * never shows it as confidently present.
 */
export function objectStateText(o: WorldObject, nowMs: number): string {
  if (o.state === "present" && objectStale(o, nowMs))
    return `Last seen ${ageLabel(objectAgeS(o, nowMs))}`;
  return (
    {
      present: "Present",
      last_seen: "Last seen",
      moved: "Moved",
      not_found_on_rescan: "Not found on rescan",
    }[o.state] ?? o.state
  );
}

export interface ScreenRect {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  /** Higher wins; `Infinity` is never hidden. */
  priority: number;
}

/**
 * Greedy label decluttering: keep labels from highest priority down and hide
 * any whose screen rectangle overlaps one already kept. Returns hidden ids.
 */
export function overlappingLabels(rects: ScreenRect[], gap = 2): Set<string> {
  const kept: ScreenRect[] = [];
  const hidden = new Set<string>();
  for (const r of [...rects].sort((a, b) => b.priority - a.priority)) {
    const hit = kept.some(
      (k) =>
        r.x < k.x + k.width + gap &&
        k.x < r.x + r.width + gap &&
        r.y < k.y + k.height + gap &&
        k.y < r.y + r.height + gap,
    );
    if (hit && r.priority !== Infinity) hidden.add(r.id);
    else kept.push(r);
  }
  return hidden;
}

/** Label priority: people first, then by evidence (confidence × frames). */
export const labelPriority = (o: WorldObject) =>
  (o.class === "person" ? 100 : 0) +
  o.confidence * Math.min(o.observations, 10);
