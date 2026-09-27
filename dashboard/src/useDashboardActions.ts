import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import {
  DEFAULT_VIEW,
  planActions,
  type ViewControls,
  type VoiceAction,
} from "./dashboardActions";
import {
  detectionAgeMs,
  detectionImageUrl,
  detectionsLive,
} from "./detections";
import {
  navAction,
  offerForMap,
  voiceCommand,
  voicePrompt,
  waitForCard,
} from "./navProposals";
import { mapKey } from "./protocol";
import type { CameraPose, SceneHandle } from "./Scene";
import type { Mission } from "./state";

interface Snapshot<P> {
  controls: ViewControls;
  selected: string | null;
  panel: P | null;
  camera: CameraPose | null;
}
const MAX_HISTORY = 20;
// How long a new rover suggestion's spoken reply waits for its card's backend check.
const CARD_CHECK_MS = 8000;

function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const stamp = (ms: number) =>
  new Date(ms).toISOString().replace(/[:.]/g, "-").slice(0, 19);

/**
 * Applies validated voice actions to this viewer only: display filters,
 * layers, 2D/3D, camera focus, the evidence panel, undo, and explicit saves of
 * the current view or the newest already received phone frame.
 */
export function useDashboardActions<P extends string>(opts: {
  mission: Mission;
  apiUrl: string;
  hasGeometry: boolean;
  selected: string | null;
  setSelected: (id: string | null) => void;
  panel: P | null;
  setPanel: (panel: P | null) => void;
  evidencePanel: P;
  scene: RefObject<SceneHandle>;
}) {
  const [controls, setControls] = useState<ViewControls>(DEFAULT_VIEW);
  const [history, setHistory] = useState<Snapshot<P>[]>([]);
  const applied = useRef(new Set<string>());
  const latest = useRef({ ...opts, controls, history });
  latest.current = { ...opts, controls, history };
  const scope = opts.mission.mapKey;
  useEffect(() => setHistory([]), [scope]);

  const camera = (pose: CameraPose | null, view: ViewControls["view"]) => {
    const handle = latest.current.scene.current;
    if (!pose || view !== "3d" || !handle) return;
    if (handle.camera) handle.camera.set(pose);
    else handle.pending = { ...handle.pending, pose };
  };

  const saveView = async (view: ViewControls["view"]) => {
    const blob = await latest.current.scene.current
      ?.capture?.()
      .catch(() => null);
    if (!blob) return "The current view could not be captured.";
    const png = blob.type === "image/png";
    download(
      blob,
      `godseye-${view}-view-${stamp(Date.now())}.${png ? "png" : "svg"}`,
    );
    return `Saved an image of the ${view.toUpperCase()} view${png ? " without labels" : ""}.`;
  };

  const saveFrame = async () => {
    const { mission, apiUrl } = latest.current;
    const d = mission.detections;
    if (!d) return "No phone camera frame has arrived yet.";
    const response = await fetch(detectionImageUrl(apiUrl, d), {
      cache: "no-store",
    }).catch(() => null);
    const type = response?.headers.get("Content-Type") ?? "";
    if (!response?.ok || !type.startsWith("image/jpeg"))
      return `Phone frame #${d.frame.frame_id} is no longer available to save.`;
    const blob = await response.blob();
    const captured = d.frame.t_wall_ms;
    download(blob, `godseye-frame-${d.frame.frame_id}-${stamp(captured)}.jpg`);
    const age = Math.round(detectionAgeMs(d, Date.now()) / 1000);
    return (
      `Saved phone frame #${d.frame.frame_id}, captured ${new Date(captured).toLocaleTimeString()}, ` +
      `received ${age} s ago${detectionsLive(d, Date.now()) ? "" : " (stale)"}. ` +
      "It is the latest received frame, not a new photo."
    );
  };

  /** Apply one reply's actions at most once, all or nothing; returns what actually happened. */
  const run = useCallback(
    async (actions: VoiceAction[], replyScope: string | null) => {
      const fresh = actions.filter((a) => !applied.current.has(a.id));
      if (!fresh.length) return "Those actions were already applied.";
      if (applied.current.size > 500) applied.current.clear();
      fresh.forEach((a) => applied.current.add(a.id));
      const now = latest.current;
      const suggestion = fresh.length === 1 ? navAction(fresh[0]) : null;
      if (suggestion) {
        // A rover suggestion changes nothing here: it only becomes a card a person must confirm.
        if (!replyScope || replyScope !== now.mission.mapKey)
          return "The map changed while Scout was answering. Nothing was suggested.";
        offerForMap(replyScope, [suggestion]);
        if (suggestion.name === "stop_navigation")
          return "Stop is on screen. Press it, or Stop in Rover controls, to stop the rover.";
        // Speak what the checked card offers and how to confirm it by voice.
        const card = await waitForCard(
          `${replyScope}:${suggestion.id}`,
          CARD_CHECK_MS,
        );
        return card
          ? voicePrompt(suggestion, card.state())
          : "I put that suggestion on screen. Nothing moves unless you confirm it there.";
      }
      const d = now.mission.detections;
      const plan = planActions(fresh, {
        scope: now.mission.mapKey,
        replyScope,
        objects: now.mission.objects,
        controls: now.controls,
        history: now.history.length,
        hasGeometry: now.hasGeometry,
        frame:
          d && mapKey(d.frame) === now.mission.mapKey
            ? {
                frameId: d.frame.frame_id,
                capturedMs: d.frame.t_wall_ms,
                ageMs: detectionAgeMs(d, Date.now()),
              }
            : null,
        nowMs: Date.now(),
      });
      if (!plan.ok) return plan.text;
      const handle = now.scene.current;
      if (plan.undo) {
        const previous = now.history.at(-1)!;
        setHistory(now.history.slice(0, -1));
        setControls(previous.controls);
        now.setSelected(previous.selected);
        now.setPanel(previous.panel);
        camera(previous.camera, previous.controls.view);
      } else {
        if (plan.reversible)
          setHistory([
            ...now.history.slice(1 - MAX_HISTORY),
            {
              controls: now.controls,
              selected: now.selected,
              panel: now.panel,
              camera:
                now.controls.view === "3d"
                  ? (handle?.camera?.get() ?? null)
                  : null,
            },
          ]);
        setControls(plan.controls);
        if (handle && (plan.focus || plan.frameRoom)) {
          const pending = {
            frame: plan.frameRoom || undefined,
            focus: plan.focus?.position,
          };
          if (handle.camera) {
            if (pending.frame) handle.camera.frame();
            if (pending.focus) handle.camera.focus(pending.focus);
          } else handle.pending = { ...handle.pending, ...pending };
        }
        if (plan.focus) now.setSelected(plan.focus.id);
        if (plan.evidence) {
          now.setSelected(plan.evidence.id);
          now.setPanel(now.evidencePanel);
        }
      }
      const lines = [...plan.lines];
      if (plan.snapshot) {
        // Let a view switch in the same reply render before capturing it.
        await new Promise((r) =>
          requestAnimationFrame(() => requestAnimationFrame(r)),
        );
        lines.push(await saveView(plan.controls.view));
      }
      if (plan.saveFrame) lines.push(await saveFrame());
      return lines.join(" ");
    },
    [],
  );

  /** A spoken "go" or "cancel", for the map the reply was grounded on only if it is still shown. */
  const runCommand = useCallback(
    (command: "confirm" | "cancel", replyScope: string | null) =>
      !replyScope || replyScope !== latest.current.mission.mapKey
        ? Promise.resolve(
            "The map changed while Scout was listening. Nothing moved.",
          )
        : voiceCommand(command, replyScope),
    [],
  );
  return { controls, setControls, run, runCommand };
}
