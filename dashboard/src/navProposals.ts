/**
 * Voice-suggested rover actions as confirmation cards. A voice reply can only
 * offer them: validation happens on the backend (`/nav/propose`), and only a
 * person's click on a card, or a spoken "go" when exactly one card is live,
 * sends `/nav/confirm`, once. A confirmed move is then measured by the phone's
 * pose (`GET /nav/move`). See backend/NAV_ACTIONS.md.
 */
import type { MissionController } from "./useMission";

export const NAV_ACTION_NAMES = [
  "propose_navigation",
  "propose_exploration",
  "stop_navigation",
  "propose_move",
] as const;
export type NavActionName = (typeof NAV_ACTION_NAMES)[number];

export interface NavAction {
  id: string;
  name: NavActionName;
  args: Record<string, unknown>;
}

export interface NavOffer {
  key: string;
  mapKey: string;
  action: NavAction;
}

export interface MoveSummary {
  direction: "forward" | "left" | "right";
  label: string;
  limits: string | null;
}

export interface NavProposal {
  proposalId: string | null;
  kind: "destination" | "exploration" | "stop" | "move";
  status: "ready" | "unavailable";
  reason: string | null;
  message: string | null;
  alternative: string | null;
  target: {
    kind: "object" | "point";
    className: string | null;
    label: string | null;
    position: [number, number];
  } | null;
  destination: [number, number] | null;
  lengthM: number | null;
  move: MoveSummary | null;
  execution: {
    available: boolean;
    reason: string | null;
    message: string | null;
    /** Why the autonomous adapter (GET /autonomy) cannot drive yet, for context only. */
    autonomyMessage: string | null;
    /** Profile caveats, e.g. the uncalibrated prototype's. */
    warnings: string[];
  };
  expiresInS: number | null;
}

const isPair = (v: unknown): v is [number, number] =>
  Array.isArray(v) && v.length === 2 && v.every(Number.isFinite);
const text = (v: unknown, limit = 400): string | null =>
  typeof v === "string" && v.length > 0 && v.length <= limit ? v : null;

const coordinate = (v: unknown) =>
  typeof v === "number" && Number.isFinite(v) && Math.abs(v) <= 50;
const exactly = (args: Record<string, unknown>, ...keys: string[]) =>
  Object.keys(args).length === keys.length && keys.every((k) => k in args);

/** Arguments exactly as the backend resolves them (`backend/nav_actions.py`). */
function navArgs(name: NavActionName, args: Record<string, unknown>) {
  if (name === "propose_move")
    return (
      exactly(args, "direction", "amount", "unit") &&
      typeof args.amount === "number" &&
      Number.isFinite(args.amount) &&
      args.amount > 0 &&
      args.amount <= 1000 &&
      (args.direction === "forward"
        ? ["cm", "m", "in"].includes(args.unit as string)
        : ["left", "right"].includes(args.direction as string) &&
          args.unit === "deg")
    );
  if (name !== "propose_navigation") return exactly(args);
  if (args.target === "point")
    return (
      exactly(args, "target", "x", "z") &&
      coordinate(args.x) &&
      coordinate(args.z)
    );
  return (
    args.target === "object" &&
    exactly(args, "target", "object_id", "class") &&
    text(args.object_id, 64) !== null &&
    text(args.class, 64) !== null
  );
}

/** Only a structurally exact navigation action is kept; anything else is dropped, never repaired. */
export function navAction(entry: unknown): NavAction | null {
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) return null;
  const { id, name, args, ...rest } = entry as Record<string, unknown>;
  if (Object.keys(rest).length) return null;
  if (typeof id !== "string" || id.length < 1 || id.length > 64) return null;
  if (!NAV_ACTION_NAMES.includes(name as NavActionName)) return null;
  if (!args || typeof args !== "object" || Array.isArray(args)) return null;
  if (!navArgs(name as NavActionName, args as Record<string, unknown>))
    return null;
  return {
    id,
    name: name as NavActionName,
    args: args as Record<string, unknown>,
  };
}

/** The backend's proposal reply, or null when it is not a well-formed v1 answer. */
export function parseProposal(data: unknown): NavProposal | null {
  if (!data || typeof data !== "object") return null;
  const v = data as Record<string, unknown>;
  if (v.version !== 1) return null;
  if (
    !["destination", "exploration", "stop", "move"].includes(v.kind as string)
  )
    return null;
  if (v.status !== "ready" && v.status !== "unavailable") return null;
  const proposalId = text(v.proposal_id, 64);
  if (v.status === "ready" && v.kind !== "stop" && !proposalId) return null;
  const execution = v.execution as Record<string, unknown> | undefined;
  if (!execution || typeof execution.available !== "boolean") return null;
  const target = v.target as Record<string, unknown> | undefined;
  const destination = isPair(v.destination) ? v.destination : null;
  if (v.status === "ready" && v.kind === "destination" && !destination)
    return null;
  const move = v.move as Record<string, unknown> | undefined;
  const moveSummary =
    move &&
    ["forward", "left", "right"].includes(move.direction as string) &&
    text(move.label, 80)
      ? {
          direction: move.direction as MoveSummary["direction"],
          label: text(move.label, 80)!,
          limits: text(move.limits),
        }
      : null;
  if (v.kind === "move" && !moveSummary) return null;
  return {
    proposalId: v.status === "ready" ? proposalId : null,
    kind: v.kind as NavProposal["kind"],
    status: v.status,
    reason: text(v.reason, 64),
    message: text(v.message),
    alternative: text(v.alternative),
    target:
      target && isPair(target.position)
        ? {
            kind: target.kind === "point" ? "point" : "object",
            className: text(target.class, 64),
            label: text(target.label, 80),
            position: target.position,
          }
        : null,
    destination,
    lengthM: Number.isFinite(v.length_m) ? (v.length_m as number) : null,
    move: moveSummary,
    execution: {
      available: execution.available,
      reason: text(execution.reason, 64),
      message: text(execution.message),
      autonomyMessage: text(execution.autonomy_message),
      warnings: Array.isArray(execution.autonomy_warnings)
        ? execution.autonomy_warnings
            .map((w) => text(w))
            .filter((w): w is string => w !== null)
            .slice(0, 3)
        : [],
    },
    expiresInS: Number.isFinite(v.expires_in_s)
      ? (v.expires_in_s as number)
      : null,
  };
}

export interface CardContext {
  /** The map the dashboard currently shows. */
  mapKey: string | null;
  /** Phone, car and detector fresh and ok, as the rover controls require. */
  healthy: boolean;
  /** Deliberately armed with no stop latched or command pending. */
  canDrive: boolean;
  mode: string | null;
  commands: boolean;
  now: number;
}

/**
 * Why a validated proposal cannot be confirmed now, or null when it can.
 * `void` reasons end the card for good (it must be asked for again); the
 * others only disable the button until the rover is ready.
 */
export function confirmBlock(
  offer: NavOffer,
  proposal: NavProposal,
  receivedAt: number,
  ctx: CardContext,
): { reason: string; void: boolean } | null {
  if (ctx.mapKey !== offer.mapKey)
    return { reason: "The map changed. Ask again.", void: true };
  if (proposal.status !== "ready" || !proposal.proposalId)
    return { reason: proposal.message ?? "Unavailable.", void: true };
  if (
    proposal.expiresInS !== null &&
    ctx.now - receivedAt > proposal.expiresInS * 1000
  )
    return { reason: "This suggestion expired. Ask again.", void: true };
  if (!ctx.commands)
    return { reason: "This feed is telemetry only.", void: false };
  if (!ctx.healthy)
    return {
      reason:
        proposal.execution.reason && !proposal.execution.available
          ? (proposal.execution.message ?? "Rover health is not ok.")
          : "Rover health is not ok.",
      void: false,
    };
  if (proposal.kind === "destination" || proposal.kind === "move") {
    const move = proposal.kind === "move";
    if (ctx.mode === "explore")
      return {
        reason: move
          ? "Leave Explore mode to make a manual move."
          : "Leave Explore mode to drive to a destination.",
        void: false,
      };
    if (!ctx.canDrive)
      return {
        reason: move
          ? "Arm for this move first; it selects Standard. Nothing moves until you confirm."
          : "Arm the rover first; confirming switches it to navigate.",
        void: false,
      };
  }
  return null;
}

export interface MoveResult {
  moveId: string;
  proposalId: string | null;
  status: "running" | "completed" | "stopped" | "failed";
  reason: string | null;
  /** The backend's measured result sentence; null while running. */
  text: string | null;
  /** Meters forward or degrees turned, from the phone's tracked pose; null before any measurement. */
  achieved: number | null;
  unit: "m" | "deg";
}

/** `GET /nav/move`'s latest move, or null when absent or malformed. */
export function parseMove(data: unknown): MoveResult | null {
  const move = (data as Record<string, unknown> | null)?.move as
    Record<string, unknown> | null | undefined;
  if ((data as Record<string, unknown> | null)?.version !== 1 || !move)
    return null;
  const moveId = text(move.move_id, 64);
  if (
    !moveId ||
    !["running", "completed", "stopped", "failed"].includes(
      move.status as string,
    ) ||
    !["m", "deg"].includes(move.requested_unit as string)
  )
    return null;
  const done = move.status !== "running";
  const result = text(move.text, 400);
  if (done && !result) return null;
  return {
    moveId,
    proposalId: text(move.proposal_id, 64),
    status: move.status as MoveResult["status"],
    reason: text(move.reason, 64),
    text: done ? result : null,
    achieved: Number.isFinite(move.achieved) ? (move.achieved as number) : null,
    unit: move.requested_unit as MoveResult["unit"],
  };
}

type Listener = () => void;
let offers: NavOffer[] = [];
const seen = new Set<string>();
const listeners = new Set<Listener>();
const MAX_OFFERS = 3;

/**
 * Hand one voice reply's actions to the confirmation cards. Call once per reply
 * with the map that reply was grounded on. Non-navigation and malformed entries
 * are ignored, and an action id already seen for that map is never shown again.
 */
export function offerNavigationActions(reply: {
  session_id: unknown;
  map_epoch: unknown;
  actions: unknown;
}) {
  if (
    typeof reply.session_id !== "string" ||
    !Number.isSafeInteger(reply.map_epoch)
  )
    return;
  if (!Array.isArray(reply.actions)) return;
  offerForMap(
    JSON.stringify([reply.session_id, reply.map_epoch]),
    reply.actions,
  );
}

/**
 * The same, for a reply scope already in the dashboard's `mapKey` form
 * (`useDashboardActions`); returns how many new suggestions were shown.
 */
export function offerForMap(mapKey: string, actions: unknown[]) {
  const fresh: NavOffer[] = [];
  for (const entry of actions) {
    const action = navAction(entry);
    if (!action) continue;
    const key = `${mapKey}:${action.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    fresh.push({ key, mapKey, action });
  }
  if (!fresh.length) return 0;
  offers = [...offers, ...fresh].slice(-MAX_OFFERS);
  listeners.forEach((listener) => listener());
  return fresh.length;
}

export function dismissOffer(key: string) {
  offers = offers.filter((offer) => offer.key !== key);
  listeners.forEach((listener) => listener());
}

export function subscribeOffers(listener: Listener) {
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

export const currentOffers = () => offers;

/** Test-only reset of the module store. */
export function resetOffers() {
  offers = [];
  seen.clear();
  cards.clear();
  listeners.forEach((listener) => listener());
}

// Spoken confirmation ---------------------------------------------------------
//
// A spoken "go" or "cancel" (classified by the backend without the answer
// model) acts only through a card's own buttons: each card registers what its
// buttons would do right now, and a command acts only when exactly one card on
// the shown map is live. Every backend check is unchanged.

/** How long a card waits at its spoken "say go to arm" prompt; the proposal lifetime. */
export const ARM_PROMPT_MS = 30000;

/** What a spoken "go" presses on a card: its confirm button, or its arm step. */
export type VoiceStep = "confirm" | "arm";

export interface CardVoiceState {
  /** Still waiting for the backend check. */
  checking: boolean;
  /** Checked and unconfirmed, or waiting at its spoken arm prompt: a command may act on it. */
  live: boolean;
  /** The one step "go" performs now, or null when something blocks it. */
  step: VoiceStep | null;
  /** Why "go" does nothing on this live card now (spoken instead). */
  blocked: string | null;
  /** Why this card is not live: voided, expired, already confirmed. */
  why: string | null;
}

export interface CardHandle {
  key: string;
  mapKey: string;
  action: NavAction;
  /** Evaluated when asked, against the latest rover state and clock. */
  state: () => CardVoiceState;
  /** Presses exactly the card's button for `step`; resolves to what happened. */
  go: (step: VoiceStep) => Promise<string>;
  /** The card's own dismiss (which cancels its proposal); returns what happened. */
  cancel: () => string;
}

const cards = new Map<string, CardHandle>();
const cardListeners = new Set<Listener>();

/** A card registers its voice handle while mounted; returns the unregister. */
export function registerCard(card: CardHandle) {
  cards.set(card.key, card);
  cardChanged();
  return () => {
    if (cards.get(card.key) === card) cards.delete(card.key);
    cardChanged();
  };
}

/** A card's phase changed: wake anything waiting for it to settle. */
export function cardChanged() {
  cardListeners.forEach((listener) => listener());
}

/** The card for `key` once its backend check finished, or null after `timeoutMs`. */
export function waitForCard(
  key: string,
  timeoutMs: number,
): Promise<CardHandle | null> {
  return new Promise((resolve) => {
    const check = () => {
      const card = cards.get(key);
      if (!card || card.state().checking) return false;
      done(card);
      return true;
    };
    const timer = setTimeout(() => done(null), timeoutMs);
    const done = (card: CardHandle | null) => {
      clearTimeout(timer);
      cardListeners.delete(listener);
      resolve(card);
    };
    const listener = () => void check();
    cardListeners.add(listener);
    check();
  });
}

const UNIT_WORDS: Record<string, string> = {
  cm: "centimeters",
  m: "meters",
  in: "inches",
  deg: "degrees",
};

/** The suggestion as a short spoken phrase. */
export function spokenTitle(action: NavAction) {
  const args = action.args;
  if (action.name === "stop_navigation") return "Stop";
  if (action.name === "propose_exploration") return "Explore";
  if (action.name === "propose_move")
    return args.direction === "forward"
      ? `Move forward ${Number(args.amount)} ${UNIT_WORDS[String(args.unit)]}`
      : `Turn ${String(args.direction)} ${Number(args.amount)} degrees`;
  return args.target === "point"
    ? "Drive to that point"
    : `Drive to the ${String(args.class)}`;
}

const STEP_WORDS: Record<string, string> = {
  exploration: "select Explore mode",
  move: "move",
  destination: "drive",
};

/** What Scout says once a new card was checked: how to confirm it by voice, or why it cannot. */
export function voicePrompt(action: NavAction, state: CardVoiceState) {
  const title = spokenTitle(action);
  if (!state.live)
    return `${title} is not available. ${state.why ?? ""} Nothing moved.`
      .replace(/\s+/g, " ")
      .trim();
  if (state.step === "arm")
    return `${title} is ready. Say go to arm for this move, or cancel.`;
  if (state.step === "confirm") {
    const kind =
      action.name === "propose_exploration"
        ? "exploration"
        : action.name === "propose_move"
          ? "move"
          : "destination";
    return `${title} is ready. Say go to ${STEP_WORDS[kind]}, or cancel.`;
  }
  return `${title} is on screen. ${state.blocked ?? ""} Say cancel to drop it.`
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Carry out a spoken "go" or "cancel" for the shown map. "go" acts only when
 * exactly one card is live, and then only through that card's own button;
 * otherwise nothing happens and the reason is returned to be spoken.
 * "cancel" dismisses every live card on the shown map (cancelling never moves).
 */
export async function voiceCommand(
  command: "confirm" | "cancel",
  mapKey: string | null,
): Promise<string> {
  const all = [...cards.values()];
  const here = all.filter((card) => mapKey !== null && card.mapKey === mapKey);
  const live = here.filter((card) => card.state().live);
  if (command === "cancel") {
    if (!live.length) return "There is no suggestion to cancel. Nothing moved.";
    const said = live.map((card) => card.cancel());
    return live.length === 1
      ? said[0]
      : `Cancelled ${live.length} suggestions. Nothing moved.`;
  }
  if (live.length > 1)
    return "More than one suggestion is on screen. Press the one you want, or say cancel. Nothing moved.";
  if (!live.length) {
    if (here.some((card) => card.state().checking))
      return "That suggestion is still being checked. Say go again in a moment. Nothing moved.";
    const latest = here.at(-1)?.state();
    if (latest?.why) return `${latest.why} Nothing moved.`;
    if (all.some((card) => card.state().live))
      return "That suggestion is for a different map. Ask again. Nothing moved.";
    return "There is nothing to confirm. Nothing moved.";
  }
  const [card] = live;
  const state = card.state();
  if (!state.step)
    return `${state.blocked ?? "That suggestion cannot be confirmed now."} Nothing moved.`;
  return card.go(state.step);
}

/** The Arm button's availability in Rover controls (`components.tsx`), shared with a spoken arm step. */
export function roverHealthy(
  controller: Pick<
    MissionController,
    "mission" | "config" | "autonomy" | "stale"
  >,
) {
  const { mission, config, autonomy, stale } = controller;
  const health = mission.health;
  return (
    !stale &&
    health?.phone === "ok" &&
    health.car === "ok" &&
    health.detector === "ok" &&
    (autonomy?.adapter !== "iphone" ||
      (autonomy.ready && health.mode !== "manual" && !!config.roverKey))
  );
}

/**
 * Why Rover controls' Arm cannot be pressed now, or null. While motion is
 * possible that button is Stop instead, so an arm step is refused then too.
 */
export function armBlock(
  controller: Pick<
    MissionController,
    "mission" | "config" | "autonomy" | "stale" | "pending" | "requiresStop"
  >,
): string | null {
  const { config, autonomy, pending } = controller;
  if (controller.requiresStop)
    return "The rover may already be armed or starting. Say stop, or wait.";
  if (!config.commands) return "This feed is telemetry only.";
  if (pending) return "Another rover command is in progress.";
  if (autonomy?.adapter === "iphone") {
    if (!(config.roverKey || config.serverPaired))
      return "Enter the rover pairing key in Connection settings.";
  } else if (!roverHealthy(controller))
    return "Waiting for healthy components.";
  return null;
}
