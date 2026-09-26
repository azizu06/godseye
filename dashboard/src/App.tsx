import { useState } from "react";
import {
  Activity,
  ArrowRight,
  Box,
  ChevronDown,
  CircleHelp,
  Crosshair,
  Download,
  Focus,
  Navigation,
  Plus,
  ScanLine,
  Settings2,
  ShieldAlert,
  Square,
  X,
} from "lucide-react";
import Scene from "./Scene";
import { useMission } from "./useMission";
import {
  Dialog,
  EventList,
  exportMission,
  Inspector,
  ObjectList,
  OperatorControls,
  Settings,
} from "./components";

type Panel = "memory" | "intelligence" | "activity" | "controls";
const panels = [
  { id: "memory", label: "Spatial memory", icon: Box },
  { id: "intelligence", label: "Object intelligence", icon: Focus },
  { id: "activity", label: "Recent activity", icon: Activity },
  { id: "controls", label: "Rover controls", icon: Navigation },
] as const;
export default function App() {
  const controller = useMission();
  const {
    mission,
    config,
    setConfig,
    connection,
    stale,
    command,
    pending,
    notice,
    notify,
    now,
    trackingFault,
    historyStatus,
    rescanBaseline,
  } = controller;
  const [panel, setPanel] = useState<Panel | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [settings, setSettings] = useState(false),
    [newSession, setNewSession] = useState(false),
    [help, setHelp] = useState(false),
    [rescanBusy, setRescanBusy] = useState(false);
  const simulated = config.source === "simulator";
  const object = mission.objects.find((o) => o.id === selected);
  const sourceLabel = simulated
    ? "Simulation"
    : mission.health?.stop_reason?.toLowerCase().includes("synthetic")
      ? "Synthetic feed"
      : "External feed";
  const select = (id: string) => {
    setSelected(id);
    setPanel("intelligence");
  };
  const rescan = async () => {
    setRescanBusy(true);
    const ok = await command("/rescan");
    if (ok && simulated) {
      setSelected("sim-backpack");
      setPanel("intelligence");
      notify("Revisiting the baseline. Watch the backpack…");
      setTimeout(() => setRescanBusy(false), 3000);
    } else setRescanBusy(false);
  };
  return (
    <main className="immersive-shell">
      <h1 className="sr-only">Godseye spatial workspace</h1>
      <Scene
        mission={mission}
        selected={selected}
        onSelect={select}
        simulated={simulated}
        canGoal={controller.canDrive && mission.health?.mode !== "explore"}
        onGoal={(x, z) => void controller.navigate(x, z)}
        onViewYaw={controller.setViewYaw}
      />
      <div
        className="workspace-actions heading-actions"
        aria-label="Workspace actions"
      >
        <button
          className={`source-button ${simulated ? "simulated" : ""}`}
          onClick={() => setSettings(true)}
          aria-label="Connection settings"
        >
          <span className="live-dot" />
          {sourceLabel}
          <ChevronDown size={13} />
        </button>
        <button className="stop-button" onClick={() => void command("/stop")}>
          <Square size={13} fill="currentColor" /> STOP ROVER
        </button>
        <button
          className="button subtle"
          aria-label="Export snapshot"
          title="Export snapshot"
          onClick={() => exportMission(controller)}
        >
          <Download size={15} />
          <span>Export snapshot</span>
        </button>
        <button
          className="button secondary"
          aria-label="New session"
          title="New session"
          onClick={() => setNewSession(true)}
        >
          <Plus size={16} />
          <span>New session</span>
        </button>
      </div>
      <div className="workspace-state">
        <span className={`state-pill ${stale ? "amber" : ""}`}>
          <i />
          {connection === "reconnecting"
            ? "Reconnecting…"
            : connection === "connecting"
              ? "Connecting…"
              : stale
                ? "Telemetry stale"
                : "Receiving telemetry"}
        </span>
        <span>
          {!stale && mission.pose?.tracking === "normal"
            ? "Tracking normal"
            : "Tracking unavailable"}
        </span>
      </div>
      <nav className="workspace-panels" aria-label="Workspace panels">
        {panels.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            className={panel === id ? "active" : ""}
            aria-label={label}
            aria-expanded={panel === id}
            onClick={() => setPanel(panel === id ? null : id)}
          >
            <Icon size={17} />
            <span>{label}</span>
            {id === "memory" && <small>{mission.objects.length}</small>}
          </button>
        ))}
        <button aria-label="Workspace help" onClick={() => setHelp(true)}>
          <CircleHelp size={17} />
        </button>
      </nav>
      {panel && (
        <aside
          className="workspace-drawer"
          aria-label={`${panels.find((p) => p.id === panel)?.label} panel`}
        >
          <div className="drawer-heading">
            <span className="eyebrow">
              {panels.find((p) => p.id === panel)?.label}
            </span>
            <button
              className="icon-button"
              aria-label="Close panel"
              onClick={() => setPanel(null)}
            >
              <X size={17} />
            </button>
          </div>
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
              />
              <p className="drawer-note">
                Objects appear as they are observed. Select one to inspect its
                evidence.
              </p>
            </section>
          )}
          {panel === "intelligence" && (
            <Inspector
              object={object}
              events={mission.events}
              now={now}
              onFocus={() => setPanel(null)}
            />
          )}
          {panel === "activity" && (
            <section className="activity-panel">
              <div className="section-heading">
                <h3 title={historyStatus}>Recent activity</h3>
                <span className="count-badge">{mission.events.length}</span>
              </div>
              <p className="drawer-note">{historyStatus}</p>
              <EventList
                events={mission.events}
                objects={mission.objects}
                onSelect={select}
              />
            </section>
          )}
          {panel === "controls" && (
            <>
              <OperatorControls controller={controller} />
              <section className="demo-banner">
                <ScanLine size={24} />
                <div>
                  <span className="eyebrow">
                    {simulated ? "SIMULATED REVISIT" : "RESCAN BASELINE"}
                  </span>
                  <h3>Observe what changed.</h3>
                  <p>
                    {simulated
                      ? "Revisit the backpack and compare its position."
                      : (rescanBaseline ??
                        "Save a baseline and watch new observations.")}
                  </p>
                  <button
                    className="button"
                    disabled={
                      rescanBusy ||
                      !!pending ||
                      mission.health?.armed ||
                      stale ||
                      (!simulated && !config.commands) ||
                      (simulated &&
                        !mission.objects.some((o) => o.id === "sim-backpack"))
                    }
                    onClick={() => void rescan()}
                  >
                    {rescanBusy
                      ? "Rescanning…"
                      : simulated
                        ? "Run relocation demo"
                        : "Start rescan"}
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
                {simulated && (
                  <button className="button subtle" onClick={trackingFault}>
                    <ShieldAlert size={14} />
                    {mission.pose?.tracking === "normal"
                      ? "Test tracking loss"
                      : "Restore tracking"}
                  </button>
                )}
              </div>
            </>
          )}
        </aside>
      )}
      {panel !== "controls" && (
        <div className="flight-controls">
          <OperatorControls controller={controller} compact />
        </div>
      )}
      <footer className="telemetry-bar">
        <span className="telemetry-brand">
          <Crosshair size={13} /> GOD’S EYE
        </span>
        <span className="footer-note">
          {simulated
            ? "SIMULATED DATA · NO HARDWARE CONNECTED"
            : (mission.health?.stop_reason ??
              "Live backend · ARKit world meters")}
        </span>
        <span>
          {mission.objects.length} objects ·{" "}
          {mission.events.filter((e) => e.kind === "moved").length} confirmed
          moves
        </span>
      </footer>
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
              Middle-drag to orbit, Shift + middle-drag to pan, and scroll to
              zoom. The visible Orbit and Pan tools work with the primary mouse
              button. Home resets the view.
            </p>
            <h3>Move through the world</h3>
            <p>
              Standard keeps arrow-key steering and click-to-navigate available
              together. Arrow directions follow your view. The rover turns
              toward the requested direction before moving forward. Release the
              keys to stop steering.
            </p>
            <p>
              Arm explicitly before moving. Stop disarms. Explore is a separate
              mode. The real car adapter remains logging-only.
            </p>
            <h3>Look a little closer</h3>
            <p>
              Open Spatial Memory, Object Intelligence, or Recent Activity to
              inspect observations. New maps start unknown and keep the surfaces
              they discover.
            </p>
          </div>
        </Dialog>
      )}
    </main>
  );
}
