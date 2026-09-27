import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Compass, MapPin, Square, X } from "lucide-react";
import type { MissionController } from "./useMission";
import { sendCommand } from "./transport";
import {
  confirmBlock,
  currentOffers,
  dismissOffer,
  offerNavigationActions,
  parseProposal,
  subscribeOffers,
  type NavOffer,
  type NavProposal,
} from "./navProposals";

type Phase =
  | { kind: "checking" }
  | { kind: "shown"; proposal: NavProposal; at: number }
  | { kind: "void"; reason: string; proposal: NavProposal | null }
  | { kind: "confirming"; proposal: NavProposal }
  | { kind: "done"; message: string; proposal: NavProposal };

const round = (v: number) => (Math.round(v * 10) / 10).toFixed(1);

function title(offer: NavOffer) {
  const args = offer.action.args;
  if (offer.action.name === "stop_navigation") return "Stop the rover";
  if (offer.action.name === "propose_exploration")
    return "Explore unmapped floor";
  if (args.target === "point")
    return `Drive to (${round(Number(args.x))}, ${round(Number(args.z))}) m`;
  return `Drive to the ${String(args.class)}`;
}

function detail(proposal: NavProposal) {
  if (proposal.kind === "exploration")
    return "Selects Explore mode, disarmed. Arm afterwards to start the frontier planner.";
  if (!proposal.destination || proposal.lengthM === null) return null;
  const what = proposal.target?.className ?? "point";
  const gap = proposal.target
    ? Math.hypot(
        proposal.destination[0] - proposal.target.position[0],
        proposal.destination[1] - proposal.target.position[1],
      )
    : 0;
  return proposal.target?.kind === "object"
    ? `Stops on clear floor ${round(gap)} m from the ${what}; route ${round(proposal.lengthM)} m.`
    : `Route ${round(proposal.lengthM)} m on mapped floor.`;
}

/**
 * Confirmation cards for rover actions suggested by a voice answer. A card
 * never acts by itself: the backend validates the suggestion, and only a
 * click here confirms it, once. Stop stays one click away on every card.
 */
export function NavigationProposals({
  controller,
}: {
  controller: MissionController;
}) {
  const offers = useSyncExternalStore(subscribeOffers, currentOffers);
  useEffect(() => {
    // The voice reply hands its actions over here; see backend/NAV_ACTIONS.md.
    const offered = (event: Event) =>
      offerNavigationActions((event as CustomEvent).detail ?? {});
    window.addEventListener("godseye:voice-actions", offered);
    return () => window.removeEventListener("godseye:voice-actions", offered);
  }, []);
  if (!offers.length) return null;
  return (
    <div className="nav-proposals" aria-label="Suggested rover actions">
      {offers.map((offer) => (
        <ProposalCard key={offer.key} offer={offer} controller={controller} />
      ))}
    </div>
  );
}

function ProposalCard({
  offer,
  controller,
}: {
  offer: NavOffer;
  controller: MissionController;
}) {
  const { mission, config, canDrive, stale, trackingNormal, now } = controller;
  const [phase, setPhase] = useState<Phase>({ kind: "checking" });
  const asked = useRef(false);
  const stopAtReceipt = useRef<string | null | undefined>(undefined);
  const wasHealthy = useRef(false);
  const healthy =
    trackingNormal &&
    !stale &&
    mission.health?.car === "ok" &&
    mission.health.detector === "ok";
  const isStop = offer.action.name === "stop_navigation";
  const pendingId = phase.kind === "shown" ? phase.proposal.proposalId : null;

  useEffect(() => {
    if (asked.current || isStop) return;
    asked.current = true; // one request per offer, even across re-renders
    const [session_id, map_epoch] = JSON.parse(offer.mapKey);
    if (!config.commands) {
      setPhase({
        kind: "void",
        reason:
          "This feed is telemetry only; configure a REST API to use suggestions.",
        proposal: null,
      });
      return;
    }
    sendCommand(
      config.apiUrl,
      "/nav/propose",
      { session_id, map_epoch, action: offer.action },
      undefined,
      config.roverKey,
    )
      .then((data) => {
        const proposal = parseProposal(data);
        if (!proposal) throw Error("Unexpected answer from the backend.");
        stopAtReceipt.current = mission.health?.stop_reason;
        setPhase(
          proposal.status === "ready"
            ? { kind: "shown", proposal, at: Date.now() }
            : {
                kind: "void",
                reason: [proposal.message, proposal.alternative]
                  .filter(Boolean)
                  .join(" "),
                proposal,
              },
        );
      })
      .catch((e: unknown) =>
        setPhase({
          kind: "void",
          reason:
            e instanceof Error ? e.message : "Could not check this suggestion.",
          proposal: null,
        }),
      );
  }, [offer, config, isStop, mission.health?.stop_reason]);

  // A stop, a health loss or a map change after the check voids the card for good.
  useEffect(() => {
    if (phase.kind !== "shown") return;
    if (healthy) wasHealthy.current = true;
    const stopped =
      mission.health?.stop_reason !== stopAtReceipt.current &&
      !!mission.health?.stop_reason &&
      mission.health.stop_reason !== "mode_change";
    const reason = stopped
      ? "The rover stopped since this was checked. Ask again."
      : wasHealthy.current && !healthy
        ? "Rover health changed since this was checked. Ask again."
        : null;
    if (reason) setPhase({ kind: "void", reason, proposal: phase.proposal });
  }, [phase, healthy, mission.health?.stop_reason]);

  const cancel = () => {
    if (pendingId)
      void sendCommand(
        config.apiUrl,
        "/nav/cancel",
        { proposal_id: pendingId },
        undefined,
        config.roverKey,
      ).catch(() => {});
  };
  useEffect(() => {
    if (phase.kind === "void" && phase.proposal?.proposalId) {
      void sendCommand(
        config.apiUrl,
        "/nav/cancel",
        { proposal_id: phase.proposal.proposalId },
        undefined,
        config.roverKey,
      ).catch(() => {});
    }
  }, [phase, config.apiUrl]);

  const block =
    phase.kind === "shown"
      ? confirmBlock(offer, phase.proposal, phase.at, {
          mapKey: mission.mapKey,
          healthy,
          canDrive,
          mode: mission.health?.mode ?? null,
          commands: config.commands,
          now,
        })
      : null;
  useEffect(() => {
    if (phase.kind === "shown" && block?.void)
      setPhase({
        kind: "void",
        reason: block.reason,
        proposal: phase.proposal,
      });
  }, [phase, block?.void, block?.reason]);

  const confirm = async () => {
    if (phase.kind !== "shown" || block || !phase.proposal.proposalId) return;
    const proposal = phase.proposal;
    const [session_id, map_epoch] = JSON.parse(offer.mapKey);
    setPhase({ kind: "confirming", proposal }); // never re-enabled: a confirmation is sent once
    const ok = await controller.confirmProposal(proposal.kind, {
      proposal_id: proposal.proposalId,
      session_id,
      map_epoch,
    });
    setPhase({
      kind: "done",
      proposal,
      message: ok
        ? proposal.kind === "exploration"
          ? "Explore mode selected. Arm the rover to start exploring."
          : "Confirmed. The rover is following the planned route."
        : "Not started. Ask again if you still want it.",
    });
  };

  const dismiss = () => {
    cancel();
    dismissOffer(offer.key);
  };
  const execution =
    phase.kind === "shown" && !phase.proposal.execution.available
      ? phase.proposal.execution.message
      : null;
  return (
    <section
      className={`nav-proposal ${phase.kind}`}
      data-testid="nav-proposal"
      aria-live="polite"
    >
      <header>
        {isStop ? (
          <Square size={13} />
        ) : offer.action.name === "propose_exploration" ? (
          <Compass size={14} />
        ) : (
          <MapPin size={14} />
        )}
        <strong>{title(offer)}</strong>
        <button aria-label="Dismiss suggestion" onClick={dismiss}>
          <X size={14} />
        </button>
      </header>
      <p className="nav-proposal-source">
        Suggested by Scout · needs your confirmation
      </p>
      {isStop ? (
        <p>Stopping disarms the rover and ends any route.</p>
      ) : phase.kind === "checking" ? (
        <p>Checking the current map…</p>
      ) : phase.kind === "void" ? (
        <p className="nav-proposal-reason" data-testid="nav-proposal-reason">
          {phase.reason}
        </p>
      ) : phase.kind === "done" ? (
        <p data-testid="nav-proposal-result">{phase.message}</p>
      ) : (
        <>
          <p>{detail(phase.proposal)}</p>
          {execution && (
            <p
              className="nav-proposal-reason"
              data-testid="nav-proposal-execution"
            >
              {execution}
            </p>
          )}
          {block && !execution && (
            <p className="nav-proposal-reason" data-testid="nav-proposal-block">
              {block.reason}
            </p>
          )}
          {phase.proposal.execution.autonomyMessage && (
            <p
              className="nav-proposal-note"
              data-testid="nav-proposal-autonomy"
            >
              {phase.proposal.execution.autonomyMessage}
            </p>
          )}
        </>
      )}
      <div className="nav-proposal-actions">
        {isStop ? (
          <button
            className="button stop-button"
            onClick={() => {
              void controller.command("/stop");
              dismissOffer(offer.key);
            }}
          >
            <Square size={11} fill="currentColor" /> Stop rover
          </button>
        ) : (
          <>
            {(phase.kind === "shown" || phase.kind === "confirming") && (
              <button
                className="button primary"
                disabled={phase.kind !== "shown" || !!block}
                title={block?.reason}
                onClick={() => void confirm()}
              >
                {phase.proposal.kind === "exploration"
                  ? "Select Explore"
                  : "Confirm drive"}
              </button>
            )}
            {controller.requiresStop && (
              <button
                className="button stop-button"
                onClick={() => void controller.command("/stop")}
              >
                <Square size={11} fill="currentColor" /> Stop
              </button>
            )}
          </>
        )}
      </div>
    </section>
  );
}
