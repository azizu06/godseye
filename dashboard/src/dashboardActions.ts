import type { WorldObject } from "./protocol";
import { className } from "./detections";
import { navAction, type NavAction } from "./navProposals";

/**
 * Typed dashboard view actions proposed by the voice backend (`/voice/ask`
 * `actions`). They change only what this viewer shows or saves: never raw
 * detections, stored observations, the phone camera or the car.
 */
export type VoiceAction =
  | { id: string; name: "filter_classes"; args: { classes: string[] } }
  | { id: string; name: "show_all_classes"; args: Record<string, never> }
  | {
      id: string;
      name: "set_layer";
      args: { layer: "boxes" | "labels"; visible: boolean };
    }
  | { id: string; name: "set_view"; args: { mode: "2d" | "3d" } }
  | {
      id: string;
      name: "focus_object" | "open_evidence";
      args: { object_id: string; class: string };
    }
  | {
      id: string;
      name:
        "frame_room" | "undo" | "download_view_snapshot" | "save_camera_frame";
      args: Record<string, never>;
    }
  /** A rover suggestion, only ever shown as a confirmation card (navProposals.ts). */
  | NavAction;

/** Viewer-only display state that voice actions may change and undo. */
export interface ViewControls {
  view: "3d" | "2d";
  /** Detector classes to show; null shows every class. */
  classes: string[] | null;
  boxes: boolean;
  labels: boolean;
}
export const DEFAULT_VIEW: ViewControls = {
  view: "3d",
  classes: null,
  boxes: true,
  labels: true,
};

export const MAX_ACTIONS = 4;
const NO_ARGS = [
  "show_all_classes",
  "frame_room",
  "undo",
  "download_view_snapshot",
  "save_camera_frame",
];
const name = (v: unknown) =>
  typeof v === "string" && v.length > 0 && v.length <= 80;
const keys = (args: Record<string, unknown>, ...expected: string[]) =>
  Object.keys(args).length === expected.length &&
  expected.every((key) => key in args);

/** The backend's already-validated actions, re-checked; anything unexpected rejects the reply. */
export function parseActions(raw: unknown): VoiceAction[] | null {
  if (raw === undefined) return [];
  if (!Array.isArray(raw) || raw.length > MAX_ACTIONS) return null;
  const valid = raw.every((item) => {
    if (!item || typeof item !== "object") return false;
    const { id, name: action, args } = item as Record<string, unknown>;
    if (!name(id) || !args || typeof args !== "object") return false;
    const a = args as Record<string, unknown>;
    if (NO_ARGS.includes(action as string)) return keys(a);
    switch (action) {
      case "filter_classes":
        return (
          keys(a, "classes") &&
          Array.isArray(a.classes) &&
          a.classes.length >= 1 &&
          a.classes.length <= 8 &&
          a.classes.every(name)
        );
      case "set_layer":
        return (
          keys(a, "layer", "visible") &&
          (a.layer === "boxes" || a.layer === "labels") &&
          typeof a.visible === "boolean"
        );
      case "set_view":
        return keys(a, "mode") && (a.mode === "2d" || a.mode === "3d");
      case "focus_object":
      case "open_evidence":
        return keys(a, "object_id", "class") && name(a.object_id);
      case "propose_navigation":
      case "propose_exploration":
      case "stop_navigation":
        return raw.length === 1 && navAction(item) !== null;
      default:
        return false;
    }
  });
  return valid ? (raw as VoiceAction[]) : null;
}

export interface CameraFrameInfo {
  frameId: number;
  capturedMs: number;
  /** Time since this viewer received it. */
  ageMs: number;
}
export interface PlanContext {
  /** The shown map, and the map the reply was grounded on. */
  scope: string | null;
  replyScope: string | null;
  objects: WorldObject[];
  controls: ViewControls;
  /** Reversible voice changes available to undo. */
  history: number;
  hasGeometry: boolean;
  frame: CameraFrameInfo | null;
  nowMs: number;
}
export type Plan =
  | { ok: false; text: string }
  | {
      ok: true;
      controls: ViewControls;
      focus: WorldObject | null;
      evidence: WorldObject | null;
      frameRoom: boolean;
      undo: boolean;
      snapshot: boolean;
      saveFrame: boolean;
      /** Whether applying it changes view state that undo can restore. */
      reversible: boolean;
      lines: string[];
    };

const plural = (cls: string) =>
  cls === "person"
    ? "people"
    : cls.endsWith("s")
      ? cls
      : `${cls === "potted plant" ? "plant" : cls}s`;
const list = (items: string[]) =>
  items.length < 2
    ? items.join("")
    : `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;

/** Whole seconds since the phone-clock `last_seen`, or null when the clock is implausible. */
export function lastSeenAge(object: WorldObject, nowMs: number) {
  const age = nowMs / 1000 - object.last_seen;
  return age >= -5 && age <= 7 * 24 * 3600
    ? Math.max(0, Math.round(age))
    : null;
}
const seen = (object: WorldObject, nowMs: number) => {
  const age = lastSeenAge(object, nowMs);
  return age === null ? "last-seen time unknown" : `last seen ${age} s ago`;
};

/**
 * Decide the whole effect of one reply against the current map before applying
 * anything: one failed precondition means nothing changes.
 */
export function planActions(actions: VoiceAction[], ctx: PlanContext): Plan {
  const fail = (text: string): Plan => ({
    ok: false,
    text: `${text} Nothing changed.`,
  });
  if (ctx.replyScope !== ctx.scope)
    return fail("The map changed while Scout was answering.");
  let controls = ctx.controls;
  const plan = {
    focus: null as WorldObject | null,
    evidence: null as WorldObject | null,
    frameRoom: false,
    undo: false,
    snapshot: false,
    saveFrame: false,
  };
  const lines: string[] = [];
  for (const action of actions) {
    switch (action.name) {
      case "filter_classes":
        controls = { ...controls, classes: action.args.classes };
        lines.push(`Showing only ${list(action.args.classes.map(plural))}.`);
        break;
      case "show_all_classes":
        controls = { ...controls, classes: null };
        lines.push("Showing all objects.");
        break;
      case "set_layer": {
        const { layer, visible } = action.args;
        controls = { ...controls, [layer]: visible };
        lines.push(
          `${layer === "boxes" ? "Boxes" : "Labels"} ${visible ? "shown" : "hidden"}.`,
        );
        break;
      }
      case "set_view":
        controls = { ...controls, view: action.args.mode };
        lines.push(`Switched to ${action.args.mode.toUpperCase()}.`);
        break;
      case "focus_object":
      case "open_evidence": {
        const target = ctx.objects.find((o) => o.id === action.args.object_id);
        if (!target)
          return fail(`That ${action.args.class} is no longer in this map.`);
        const label = className(target.class);
        if (action.name === "focus_object") {
          plan.focus = target;
          controls = { ...controls, view: "3d" };
          lines.push(`Focused on ${label}, ${seen(target, ctx.nowMs)}.`);
        } else {
          plan.evidence = target;
          lines.push(`Opened ${label} evidence; ${seen(target, ctx.nowMs)}.`);
        }
        break;
      }
      case "frame_room":
        if (!ctx.hasGeometry) return fail("No scan to frame yet.");
        plan.frameRoom = true;
        controls = { ...controls, view: "3d" };
        lines.push("Framed the whole scan.");
        break;
      case "undo":
        if (!ctx.history) return fail("There is no voice view change to undo.");
        plan.undo = true;
        lines.push("Undid the last voice view change.");
        break;
      case "download_view_snapshot":
        plan.snapshot = true;
        break;
      case "save_camera_frame":
        if (!ctx.frame) return fail("No phone camera frame has arrived yet.");
        plan.saveFrame = true;
        break;
    }
  }
  const reversible =
    !plan.undo &&
    (controls !== ctx.controls ||
      !!plan.focus ||
      !!plan.evidence ||
      plan.frameRoom);
  return { ok: true, controls, ...plan, reversible, lines };
}

/**
 * Objects a class filter hides (the selected object is always shown), so hidden
 * people are never silently invisible. The 3D evidence policy applies afterwards.
 */
export function hiddenByFilter(
  objects: WorldObject[],
  classes: string[] | null,
  selected: string | null = null,
) {
  const hidden = classes
    ? objects.filter((o) => o.id !== selected && !classes.includes(o.class))
    : [];
  return {
    count: hidden.length,
    people: hidden.filter((o) => o.class === "person").length,
  };
}
