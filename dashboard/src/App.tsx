import { useColorSurfaces } from "./useColorSurfaces";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  ArrowRight,
  Box,
  CircleHelp,
  Download,
  Focus,
  Navigation,
  Plus,
  ScanLine,
  Settings2,
  X,
} from "lucide-react";
import Scene, { type SceneHandle } from "./Scene";
import { SpokenEvent } from "./SpokenEvent";
import { VoiceAsk } from "./VoiceAsk";
import { PhoneControls } from "./PhoneControls";
import { DetectionOverlay } from "./DetectionOverlay";
import { detectionsLive, liveMarkers } from "./detections";
import { ApproachRouteCard, useApproachRoute } from "./ApproachRoutePanel";
import { useMission } from "./useMission";
import { useDashboardActions } from "./useDashboardActions";
import { hiddenByFilter } from "./dashboardActions";
import { className } from "./detections";
import {
  Dialog,
  EventList,
  exportMission,
  Inspector,
  ObjectList,
  OperatorControls,
  Settings,
} from "./components";

type Panel =
  "workspace" | "scene" | "memory" | "intelligence" | "activity" | "controls";
const panels = [
  { id: "memory", label: "Spatial memory", icon: Box },
  { id: "intelligence", label: "Object intelligence", icon: Focus },
  { id: "activity", label: "Recent activity", icon: Activity },
  { id: "controls", label: "Rover controls", icon: Navigation },
  { id: "scene", label: "Scene settings", icon: Settings2 },
] as const;
export default function App() {
  const controller = useMission();
  const {
    mission,
    config,
    setConfig,
    connection,
    stale,
    trackingNormal,
    command,
    pending,
    notice,
    notify,
    now,
    historyStatus,
    rescanBaseline,
  } = controller;
  const [panel, setPanel] = useState<Panel | null>(null);
  const [sceneToolsHost, setSceneToolsHost] = useState<HTMLDivElement | null>(
    null,
  );
  const [selected, setSelected] = useState<string | null>(null);
  const [settings, setSettings] = useState(false),
    [newSession, setNewSession] = useState(false),
    [help, setHelp] = useState(false),
    [rescanBusy, setRescanBusy] = useState(false);
  const drawerRef = useRef<HTMLElement>(null);
  const restoreFocus = useRef<HTMLElement | null>(null);
  const touchPointers = useRef(new Set<number>());
  const releaseRef = useRef(controller.release);
  releaseRef.current = controller.release;
  const gesture = useRef<{
    x: number;
    y: number;
    moved: boolean;
    pointer: number;
    button: number;
    contextRequested: boolean;
    ended: number;
  } | null>(null);
  const pressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const suppressCanvasClick = useRef(false);
  const clearPress = useCallback(() => {
    if (pressTimer.current) clearTimeout(pressTimer.current);
    pressTimer.current = null;
  }, []);
  const openWorkspace = useCallback(() => {
    if (!drawerRef.current)
      restoreFocus.current = document.activeElement as HTMLElement;
    releaseRef.current();
    setPanel("workspace");
  }, []);
  const closePanel = useCallback(() => {
    releaseRef.current();
    setPanel(null);
    requestAnimationFrame(() => {
      const previous = restoreFocus.current;
      if (previous?.isConnected && previous !== document.body)
        previous.focus({ preventScroll: true });
      else
        document
          .querySelector<HTMLButtonElement>(
            ".scene-toolbar button[aria-pressed=true]",
          )
          ?.focus({ preventScroll: true });
    });
  }, []);
  useEffect(() => {
    if (!panel) return;
    releaseRef.current();
    drawerRef.current
      ?.querySelector<HTMLButtonElement>("button")
      ?.focus({ preventScroll: true });
  }, [panel]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (document.querySelector("dialog[open]")) return;
      if (
        event.key !== "Escape" &&
        target.closest(
          "input, textarea, select, [contenteditable]:not([contenteditable=false])",
        )
      )
        return;
      if (
        event.key === "ContextMenu" ||
        (event.shiftKey && event.key === "F10")
      ) {
        event.preventDefault();
        openWorkspace();
      } else if (event.key === "Escape") {
        event.preventDefault();
        if (panel) closePanel();
        else openWorkspace();
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [panel, openWorkspace, closePanel]);
  useEffect(() => {
    const cancelGesture = () => {
      clearPress();
      touchPointers.current.clear();
      gesture.current = null;
    };
    window.addEventListener("blur", cancelGesture);
    document.addEventListener("visibilitychange", cancelGesture);
    return () => {
      cancelGesture();
      window.removeEventListener("blur", cancelGesture);
      document.removeEventListener("visibilitychange", cancelGesture);
    };
  }, [clearPress]);
  const capture = useColorSurfaces(
    config,
    mission.mapKey,
    connection === "connected" && controller.mapConfirmed,
    controller.ingestCaptured,
  );
  const surfaces = capture.patches;
  const persistent = capture.persistent;
  const mapCellM = capture.cellM;
  const object = mission.objects.find((o) => o.id === selected);
  const sceneHandle = useRef<SceneHandle>({});
  const actions = useDashboardActions({
    mission,
    apiUrl: config.apiUrl,
    hasGeometry:
      controller.cloud.count > 0 || !!persistent || surfaces.length > 0,
    selected,
    setSelected,
    panel,
    setPanel: (next) => (next ? setPanel(next) : closePanel()),
    evidencePanel: "intelligence",
    scene: sceneHandle,
  });
  const { controls } = actions;
  // The class filter narrows only what is drawn; stored objects and raw detections stay intact.
  const shownMission = useMemo(
    () =>
      controls.classes
        ? {
            ...mission,
            objects: mission.objects.filter((o) =>
              controls.classes!.includes(o.class),
            ),
          }
        : mission,
    [mission, controls.classes],
  );
  const hidden = hiddenByFilter(mission.objects, controls.classes);
  const detectionsFresh = detectionsLive(mission.detections, now);
  const live = useMemo(
    () =>
      liveMarkers(mission.detections, now).filter(
        (d) => !controls.classes || controls.classes.includes(d.class),
      ),
    // Recompute only for new detector output, when it turns stale, or for a new filter.
    [mission.detections, detectionsFresh, controls.classes],
  );
  const approach = useApproachRoute(config.apiUrl, mission);
  const routeResult =
    "result" in approach.route ? approach.route.result : undefined;
  const approachDrawing =
    "start" in approach.route
      ? {
          start: approach.route.start,
          points: routeResult?.status === "ok" ? routeResult.points : null,
          approach: routeResult?.status === "ok" ? routeResult.approach : null,
        }
      : null;
  const pickingRouteStart = approach.route.phase === "picking";
  const select = (id: string) => {
    setSelected(id);
    setPanel("intelligence");
  };
  const rescan = async () => {
    setRescanBusy(true);
    await command("/rescan");
    setRescanBusy(false);
  };
  return (
    <main
      className="immersive-shell minimal-workspace"
      onPointerDownCapture={(event) => {
        suppressCanvasClick.current = false;
        if (event.pointerType === "touch")
          touchPointers.current.add(event.pointerId);
        clearPress();
        if (touchPointers.current.size > 1) return;
        if (
          (event.target as HTMLElement).closest(
            "button, input, label, aside, dialog, [role=button]",
          )
        )
          return;
        clearPress();
        gesture.current = {
          x: event.clientX,
          y: event.clientY,
          moved: false,
          pointer: event.pointerId,
          button: event.button,
          contextRequested: false,
          ended: 0,
        };
        if (event.pointerType === "touch")
          pressTimer.current = setTimeout(() => {
            suppressCanvasClick.current = true;
            openWorkspace();
          }, 550);
      }}
      onPointerMoveCapture={(event) => {
        const start = gesture.current;
        if (
          start?.pointer === event.pointerId &&
          Math.hypot(event.clientX - start.x, event.clientY - start.y) > 6
        ) {
          start.moved = true;
          clearPress();
        }
      }}
      onPointerUpCapture={(event) => {
        touchPointers.current.delete(event.pointerId);
        clearPress();
        const start = gesture.current;
        if (start?.pointer === event.pointerId) {
          start.ended = Date.now();
          if (start.button === 2 && start.contextRequested && !start.moved)
            openWorkspace();
        }
      }}
      onPointerCancelCapture={(event) => {
        touchPointers.current.delete(event.pointerId);
        clearPress();
      }}
      onContextMenu={(event) => {
        if (
          (event.target as HTMLElement).closest(
            "button, input, label, aside, dialog, [role=button]",
          )
        )
          return;
        event.preventDefault();
        if (touchPointers.current.size > 1) return;
        const start = gesture.current;
        // Chromium emits contextmenu on right-button down, before a pan.
        if (start?.button === 2 && !start.ended) {
          start.contextRequested = true;
          return;
        }
        if (start?.moved && (!start.ended || Date.now() - start.ended < 600))
          return;
        openWorkspace();
      }}
      onClickCapture={(event) => {
        if (
          suppressCanvasClick.current &&
          (event.target as HTMLElement).closest(".scene-canvas")
        ) {
          suppressCanvasClick.current = false;
          event.preventDefault();
          event.stopPropagation();
        }
      }}
    >
      <h1 className="sr-only">Godseye spatial workspace</h1>
      <Scene
        toolsHost={sceneToolsHost}
        feedLabel={`External feed · ${config.wsUrl} · ${connection}`}
        cloud={controller.cloud}
        surfaceReason={capture.reason}
        mission={shownMission}
        surfaces={surfaces}
        persistentSurface={persistent}
        mapCellM={mapCellM}
        surfaceStatus={capture.status}
        selected={selected}
        onSelect={select}
        canGoal={
          controller.canDrive &&
          !pickingRouteStart &&
          mission.health?.mode !== "explore" &&
          !panel &&
          !settings &&
          !newSession &&
          !help
        }
        onGoal={(x, z) => void controller.navigate(x, z)}
        liveDetections={live}
        approachRoute={approachDrawing}
        now={now}
        pickingRouteStart={pickingRouteStart && !panel}
        onRouteStart={approach.pickStart}
        view={controls.view}
        onView={(view) => actions.setControls((c) => ({ ...c, view }))}
        showBoxes={controls.boxes}
        showLabels={controls.labels}
        handle={sceneHandle}
      />
      <ApproachRouteCard
        state={approach.route}
        personLastSeen={approach.person?.last_seen ?? null}
        now={now}
        onClear={approach.clear}
      />
      <DetectionOverlay
        detections={mission.detections}
        apiUrl={config.apiUrl}
        now={now}
        classes={controls.boxes ? controls.classes : []}
      />
      <button
        className="workspace-launcher"
        onClick={openWorkspace}
        aria-label="Open workspace"
        aria-keyshortcuts="Escape Shift+F10"
        title="Workspace · Escape, right-click or long-press"
      >
        Open workspace
      </button>
      {panel && (
        <aside
          className="workspace-drawer"
          ref={drawerRef}
          role="dialog"
          aria-label={
            panel === "workspace"
              ? "Workspace"
              : `${panels.find((p) => p.id === panel)?.label} panel`
          }
        >
          <div className="drawer-heading">
            {panel !== "workspace" && (
              <button
                className="icon-button"
                aria-label="Workspace menu"
                onClick={openWorkspace}
              >
                <ArrowRight size={16} style={{ transform: "rotate(180deg)" }} />
              </button>
            )}
            <span className="eyebrow">
              {panel === "workspace"
                ? "Workspace"
                : panels.find((p) => p.id === panel)?.label}
            </span>
            <button
              className="icon-button"
              aria-label="Close panel"
              onClick={closePanel}
            >
              <X size={17} />
            </button>
          </div>
          {panel === "workspace" && (
            <div className="workspace-menu">
              <div className="workspace-summary">
                <span className="eyebrow">Backend feed</span>
                <p>
                  {mission.health?.stop_reason ??
                    "Backend observations · ARKit world meters"}
                </p>
                <p>
                  {connection === "reconnecting"
                    ? "Reconnecting…"
                    : connection === "connecting"
                      ? "Connecting…"
                      : stale
                        ? "Telemetry stale"
                        : "Receiving telemetry"}{" "}
                  ·{" "}
                  {trackingNormal ? "Tracking normal" : "Tracking unavailable"}
                </p>
              </div>
              <nav
                className="workspace-menu-panels"
                aria-label="Workspace panels"
              >
                {panels.map(({ id, label, icon: Icon }) => (
                  <button
                    key={id}
                    onClick={() => setPanel(id)}
                    aria-label={label}
                  >
                    <Icon size={17} />
                    <span>{label}</span>
                    <ArrowRight size={14} />
                  </button>
                ))}
              </nav>
              <div className="workspace-menu-actions">
                <button
                  onClick={() => setSettings(true)}
                  aria-label="Connection settings"
                >
                  <Settings2 size={16} /> Connection settings
                </button>
                <button
                  onClick={() =>
                    exportMission(controller, persistent, mapCellM)
                  }
                  aria-label="Export snapshot"
                >
                  <Download size={16} /> Export snapshot
                </button>
                <button
                  onClick={() => setNewSession(true)}
                  aria-label="New session"
                >
                  <Plus size={16} /> New session
                </button>
                <button
                  onClick={() => setHelp(true)}
                  aria-label="Workspace help"
                >
                  <CircleHelp size={16} /> Workspace help
                </button>
              </div>
            </div>
          )}
          {panel === "scene" && (
            <div ref={setSceneToolsHost} className="scene-tools-host" />
          )}
          {panel === "memory" && (
            <section className="objects-panel">
              <div className="section-heading">
                <h2>Spatial memory</h2>
                <span className="count-badge">{mission.objects.length}</span>
              </div>
              <ObjectList
                objects={mission.objects}
                selected={selected}
                onSelect={select}
                now={now}
              />
              <p className="drawer-note">
                Every stored detection is listed here. The 3D view hides
                low-evidence objects (under 2 frames or 50%) except possible
                people; turn them on under Scene layers.
              </p>
            </section>
          )}
          {panel === "intelligence" && (
            <Inspector
              object={object}
              events={mission.events}
              now={now}
              onFocus={closePanel}
              onApproach={() => {
                if (!object) return;
                approach.begin(object.id);
                closePanel();
              }}
            />
          )}
          {panel === "activity" && (
            <section className="activity-panel">
              <div className="section-heading">
                <h3 title={historyStatus}>Recent activity</h3>
                <span className="count-badge">{mission.events.length}</span>
              </div>
              <p className="drawer-note">{historyStatus}</p>
              <SpokenEvent
                config={config}
                scope={mission.mapKey}
                events={mission.events}
              />
              <EventList
                events={mission.events}
                objects={mission.objects}
                onSelect={select}
              />
            </section>
          )}
          {panel === "controls" && (
            <>
              <PhoneControls
                config={config}
                onStop={() => void controller.command("/stop")}
              />
              <OperatorControls controller={controller} />
              <section className="rescan-banner">
                <ScanLine size={24} />
                <div>
                  <span className="eyebrow">RESCAN BASELINE</span>
                  <h3>Observe what changed.</h3>
                  <p>
                    {rescanBaseline ??
                      "Save a baseline and watch new observations."}
                  </p>
                  <button
                    className="button"
                    disabled={
                      rescanBusy ||
                      !!pending ||
                      mission.health?.armed ||
                      stale ||
                      !config.commands
                    }
                    onClick={() => void rescan()}
                  >
                    {rescanBusy ? "Rescanning…" : "Start rescan"}
                    <ArrowRight size={14} />
                  </button>
                </div>
              </section>
              <div className="drawer-tools">
                <button
                  className="button subtle"
                  onClick={() => setSettings(true)}
                >
                  <Settings2 size={14} /> Connection settings
                </button>
              </div>
            </>
          )}
        </aside>
      )}
      <VoiceAsk config={config} onActions={actions.run}>
        {(controls.classes || !controls.boxes || !controls.labels) && (
          <div className="view-filter" data-testid="view-filter">
            <span>
              {controls.classes
                ? `Showing ${controls.classes.map(className).join(", ")}`
                : "Showing all classes"}
              {!controls.boxes && " · boxes hidden"}
              {!controls.labels && " · labels hidden"}
              {hidden.count > 0 && ` · ${hidden.count} hidden`}
              {hidden.people > 0 &&
                ` (${hidden.people} ${hidden.people === 1 ? "person" : "people"})`}
            </span>
            <button
              onClick={() =>
                actions.setControls((c) => ({
                  ...c,
                  classes: null,
                  boxes: true,
                  labels: true,
                }))
              }
            >
              Show all
            </button>
          </div>
        )}
      </VoiceAsk>
      {notice && (
        <div className="toast" role="status">
          <span>{notice}</span>
          <button aria-label="Dismiss notice" onClick={() => notify("")}>
            <X size={15} />
          </button>
        </div>
      )}
      {settings && (
        <Settings
          onStop={() => void command("/stop")}
          config={config}
          onSave={async (next) => {
            controller.release();
            if (
              config.source === "external" &&
              config.commands &&
              mission.health?.armed
            ) {
              if (!(await command("/stop"))) return false;
            }
            setConfig(next);
            setSelected(null);
            return true;
          }}
          onClose={() => setSettings(false)}
        />
      )}
      {newSession && (
        <Dialog
          onStop={() => void command("/stop")}
          title="Start a fresh perspective?"
          onClose={() => setNewSession(false)}
        >
          <p className="dialog-intro">
            This clears the current map and dashboard history. Export a snapshot
            first if you want to keep this view.
          </p>
          <div className="dialog-footer">
            <button
              className="button subtle"
              onClick={() => setNewSession(false)}
            >
              Keep this session
            </button>
            <button
              className="button primary"
              disabled={!!pending}
              onClick={async () => {
                if (await command("/session")) {
                  setSelected(null);
                  setNewSession(false);
                }
              }}
            >
              Start new session <ArrowRight size={15} />
            </button>
          </div>
        </Dialog>
      )}
      {help && (
        <Dialog
          onStop={() => void command("/stop")}
          title="A spatial memory for your world"
          onClose={() => setHelp(false)}
        >
          <div className="help-content">
            <h3>Find your perspective</h3>
            <p>
              Left-drag to pan, right-drag to rotate, and scroll to zoom.
              Middle-drag also orbits; Shift + middle-drag pans. Home resets the
              view. Click without dragging to navigate; right-click without
              dragging opens the workspace.
            </p>
            <h3>Move through the world</h3>
            <p>
              Standard keeps arrow-key steering and click-to-navigate available
              together. Up moves straight ahead. Down turns around, and Left or
              Right turns a quarter turn before moving. Directions are relative
              to the rover when you press the key; orbiting the view does not
              steer it. Release the key to stop.
            </p>
            <p>
              Arm explicitly before moving. Stop disarms. Explore is a separate
              mode. The real car adapter remains logging-only.
            </p>
            <h3>Look a little closer</h3>
            <p>
              Press Escape or Shift+F10, right-click without dragging, or
              long-press the canvas to open the workspace. Spatial Memory,
              Object Intelligence and Recent Activity keep your observations;
              Scene settings holds layers and scan details. New maps start
              unknown and keep the surfaces they discover.
            </p>
          </div>
        </Dialog>
      )}
    </main>
  );
}
