/**
 * Voice-suggested rover actions as confirmation cards. A voice reply can only
 * offer them: validation happens on the backend (`/nav/propose`), and only a
 * person's click on a card sends `/nav/confirm`, once. See backend/NAV_ACTIONS.md.
 */

export const NAV_ACTION_NAMES = [
  "propose_navigation",
  "propose_exploration",
  "stop_navigation",
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

export interface NavProposal {
  proposalId: string | null;
  kind: "destination" | "exploration" | "stop";
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
  if (!["destination", "exploration", "stop"].includes(v.kind as string))
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
  if (proposal.kind === "destination") {
    if (ctx.mode === "explore")
      return {
        reason: "Leave Explore mode to drive to a destination.",
        void: false,
      };
    if (!ctx.canDrive)
      return {
        reason: "Arm the rover first; confirming switches it to navigate.",
        void: false,
      };
  }
  return null;
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
  listeners.forEach((listener) => listener());
}
