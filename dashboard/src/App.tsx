import { useState } from "react";
import {
  Activity,
  ArrowRight,
  Box,
  Check,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Clock3,
  Cpu,
  Download,
  Eye,
  Focus,
  LayoutDashboard,
  MapPin,
  Plus,
  Radio,
  ScanLine,
  Settings2,
  ShieldAlert,
  Smartphone,
  Square,
  Wifi,
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

export default function App() {
  const controller = useMission();
  const {
    mission,
    rescanBaseline,
    historyStatus,
    config,
    setConfig,
    connection,
    stale,
    command,
    pending,
    notice,
    notify,
    now,
    startedAt,
    trackingFault,
  } = controller;
  const [tab, setTab] = useState<"overview" | "objects" | "activity">(
      "overview",
    ),
    [selected, setSelected] = useState<string | null>("sim-backpack"),
    [settings, setSettings] = useState(false),
    [newSession, setNewSession] = useState(false),
    [help, setHelp] = useState(false);
  const [rescanBusy, setRescanBusy] = useState(false);
  const simulated = config.source === "simulator",
    object = mission.objects.find((o) => o.id === selected),
    moves = mission.events.filter((e) => e.kind === "moved");
  const elapsed = Math.max(0, Math.floor((now - startedAt) / 1000)),
    duration = `${String(Math.floor(elapsed / 60)).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")}`;
  const select = (id: string) => setSelected(id);
  const rescan = async () => {
    setRescanBusy(true);
    const ok = await command("/rescan");
    if (ok) {
      setSelected(simulated ? "sim-backpack" : selected);
      setTab("overview");
      if (simulated) notify("Revisiting the baseline. Watch the backpack…");
    }
    if (simulated && ok) setTimeout(() => setRescanBusy(false), 3000);
    else setRescanBusy(false);
  };
  const sourceLabel = simulated
    ? "Simulation"
    : mission.health?.stop_reason?.toLowerCase().includes("synthetic")
      ? "Synthetic feed"
      : "External feed";
  return (
    <div className="app-shell">
      <aside className="nav-rail">
        <a
          className="brand-mark"
          href="#"
          aria-label="God's Eye overview"
          onClick={(e) => {
            e.preventDefault();
            setTab("overview");
          }}
        >
          <Eye size={25} />
        </a>
        <div className="rail-divider" />
        <nav aria-label="Workspace">
          <button
            className={tab === "overview" ? "active" : ""}
            title="Overview"
            aria-label="Overview"
            onClick={() => setTab("overview")}
          >
            <LayoutDashboard size={21} />
          </button>
          <button
            className={tab === "objects" ? "active" : ""}
            title="Object inventory"
            aria-label="Object inventory"
            onClick={() => setTab("objects")}
          >
            <Box size={21} />
          </button>
          <button
            className={tab === "activity" ? "active" : ""}
            title="Activity"
            aria-label="Activity"
            onClick={() => setTab("activity")}
          >
            <Activity size={21} />
            {moves.length > 0 && <i className="rail-notification" />}
          </button>
        </nav>
        <div className="rail-bottom">
          <button
            title="Workspace help"
            aria-label="Workspace help"
            onClick={() => setHelp(true)}
          >
            <CircleHelp size={20} />
          </button>
          <button
            title="Connection settings"
            aria-label="Connection settings"
            onClick={() => setSettings(true)}
          >
            <Settings2 size={20} />
          </button>
          <div className="avatar">GE</div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="brand">
            <span>GOD’S EYE</span>
            <span className="brand-separator" />
            <small>Spatial intelligence</small>
          </div>
          <div className="topbar-right">
            <button
              className={`source-button ${simulated ? "simulated" : ""}`}
              onClick={() => setSettings(true)}
            >
              <span className="live-dot" />
              {sourceLabel}
              <ChevronDown size={13} />
            </button>
            <span className="header-divider" />
            <button
              className="stop-button"
              onClick={() => void command("/stop")}
            >
              <Square size={13} fill="currentColor" /> STOP ROVER
            </button>
          </div>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <div className="breadcrumb">
                WORKSPACE <ChevronRight size={11} /> SESSION 01
              </div>
              <h1>
                {tab === "overview"
                  ? "A room. Remembered."
                  : tab === "objects"
                    ? "Everything, in its place."
                    : "Every change tells a story."}
              </h1>
              <p>
                {tab === "overview"
                  ? "See your space. Understand what changed."
                  : tab === "objects"
                    ? "A living inventory of the objects in your world."
                    : "An observation history of your spatial world."}
              </p>
            </div>
            <div className="heading-actions">
              <button
                className="button subtle"
                onClick={() => exportMission(controller)}
              >
                <Download size={15} />
                Export snapshot
              </button>
              <button
                className="button secondary"
                onClick={() => setNewSession(true)}
              >
                <Plus size={16} />
                New session
              </button>
            </div>
          </div>
          <div className="status-strip">
            <div className="session-status">
              <span className={`status-orb ${stale ? "warn" : ""}`}>
                <Radio size={17} />
              </span>
              <div>
                <strong>
                  {simulated
                    ? "The Studio"
                    : sourceLabel === "Synthetic feed"
                      ? "Synthetic session"
                      : "Live workspace"}
                </strong>
                <span>
                  {stale
                    ? connection === "connected"
                      ? "Telemetry stale"
                      : connection === "reconnecting"
                        ? "Reconnecting…"
                        : connection === "connecting"
                          ? "Connecting…"
                          : "Disconnected"
                    : simulated
                      ? "Local simulation · no hardware"
                      : "Receiving telemetry"}
                </span>
              </div>
            </div>
            <div className="strip-divider" />
            <div className="stat-block">
              <Box size={16} />
              <div>
                <span>OBJECTS</span>
                <strong>
                  {mission.objects.length}
                  <small> recognized</small>
                </strong>
              </div>
            </div>
            <div className="stat-block">
              <ScanLine size={17} />
              <div>
                <span>CHANGES</span>
                <strong className={moves.length ? "amber-text" : ""}>
                  {moves.length}
                  <small>
                    {" "}
                    {moves.length === 1 ? "relocation" : "relocations"}
                  </small>
                </strong>
              </div>
            </div>
            <div className="stat-block">
              <Focus size={17} />
              <div>
                <span>TRACKING</span>
                <strong
                  className={
                    !stale && mission.pose?.tracking === "normal"
                      ? "mint-text"
                      : "amber-text"
                  }
                >
                  {stale
                    ? "Unavailable"
                    : mission.pose?.tracking === "normal"
                      ? "Normal"
                      : mission.pose?.tracking === "limited"
                        ? "Limited"
                        : "Unavailable"}
                </strong>
              </div>
            </div>
            <div className="stat-block session-clock">
              <Clock3 size={16} />
              <div>
                <span>SESSION TIME</span>
                <strong>{duration}</strong>
              </div>
            </div>
          </div>
          <div className={`main-grid ${tab !== "overview" ? "alternate" : ""}`}>
            <div className="main-column">
              {tab === "overview" ? (
                <>
                  <Scene
                    mission={mission}
                    selected={selected}
                    onSelect={select}
                    simulated={simulated}
                    canGoal={
                      controller.canDrive && mission.health?.mode === "navigate"
                    }
                    onGoal={(x, z) => void command("/goal", { x, z })}
                  />
                  <div
                    className={`demo-banner ${moves.length ? "changed" : ""}`}
                  >
                    <div className="demo-icon">
                      <ScanLine size={23} />
                    </div>
                    <div>
                      <span className="eyebrow">
                        {simulated
                          ? "EXPLORE SPATIAL MEMORY"
                          : "REVISIT YOUR SPACE"}
                      </span>
                      <h3>
                        {moves.length
                          ? "Same object. A new chapter."
                          : "What if something moved?"}
                      </h3>
                      <p>
                        {simulated
                          ? "Rescan the room to discover the backpack in a new location."
                          : (rescanBaseline ??
                            "Compare new observations with a saved baseline.")}
                      </p>
                    </div>
                    <button
                      className="button"
                      disabled={
                        rescanBusy ||
                        !!pending ||
                        mission.health?.armed ||
                        stale ||
                        (!simulated && !config.commands)
                      }
                      onClick={() => void rescan()}
                    >
                      {rescanBusy
                        ? "Rescanning…"
                        : simulated
                          ? "Run relocation demo"
                          : "Start rescan"}
                      {rescanBusy ? (
                        <ScanLine size={16} className="spin" />
                      ) : (
                        <ArrowRight size={16} />
                      )}
                    </button>
                  </div>
                  <OperatorControls controller={controller} />
                </>
              ) : (
                <section className="collection-panel">
                  <div className="section-heading">
                    <div>
                      {tab === "objects" ? (
                        <Box size={17} />
                      ) : (
                        <Activity size={17} />
                      )}
                      <h3>
                        {tab === "objects"
                          ? "Object inventory"
                          : "Session activity"}
                      </h3>
                      <span className="count-badge">
                        {tab === "objects"
                          ? mission.objects.length
                          : mission.events.length}
                      </span>
                    </div>
                    <span className="muted small">
                      {simulated
                        ? "Simulated observations"
                        : "Received this connection"}
                    </span>
                  </div>
                  {tab === "objects" ? (
                    <ObjectList
                      full
                      objects={mission.objects}
                      selected={selected}
                      onSelect={select}
                    />
                  ) : (
                    <EventList
                      events={mission.events}
                      objects={mission.objects}
                      onSelect={(id) => {
                        select(id);
                      }}
                    />
                  )}
                </section>
              )}
              <section className="activity-panel">
                <div className="section-heading">
                  <div>
                    <Activity size={16} />
                    <h3 title={historyStatus}>Recent activity</h3>
                    <span className="count-badge">{mission.events.length}</span>
                  </div>
                  <button
                    className="text-button"
                    onClick={() => setTab("activity")}
                  >
                    View all <ArrowRight size={13} />
                  </button>
                </div>
                <EventList
                  compact
                  events={mission.events}
                  objects={mission.objects}
                  onSelect={(id) => {
                    select(id);
                    setTab("overview");
                  }}
                />
              </section>
            </div>
            <aside className="right-column">
              <section className="objects-panel">
                <div className="section-heading">
                  <div>
                    <Box size={16} />
                    <h3>Spatial memory</h3>
                    <span className="count-badge">
                      {mission.objects.length}
                    </span>
                  </div>
                  <span className="live-dot" />
                </div>
                <ObjectList
                  objects={mission.objects}
                  selected={selected}
                  onSelect={select}
                />
              </section>
              <section className="inspector-panel">
                <Inspector
                  object={object}
                  events={mission.events}
                  now={now}
                  onFocus={() => setTab("overview")}
                />
              </section>
            </aside>
          </div>
          <footer className="workspace-footer">
            <div className="health-items">
              {(
                [
                  { key: "phone", label: "iPhone", icon: Smartphone },
                  { key: "detector", label: "Perception", icon: Cpu },
                  { key: "car", label: "Rover", icon: Wifi },
                ] as const
              ).map(({ key, label, icon: Icon }) => (
                <span
                  key={key}
                  title={`${label}: ${stale ? "unavailable" : (mission.health?.[key] ?? "down")}`}
                >
                  <Icon size={12} />
                  {label}
                  <i
                    className={
                      !stale && mission.health?.[key] === "ok" ? "ok" : "down"
                    }
                  />
                </span>
              ))}
            </div>
            <span className="footer-note">
              {simulated
                ? "SIMULATED DATA · NO HARDWARE CONNECTED"
                : mission.health?.stop_reason || "ARKit world frame · v1"}
            </span>
            {simulated ? (
              <button className="text-button" onClick={trackingFault}>
                {mission.pose?.tracking === "normal" ? (
                  <ShieldAlert size={12} />
                ) : (
                  <Check size={12} />
                )}{" "}
                {mission.pose?.tracking === "normal"
                  ? "Test tracking loss"
                  : "Restore tracking"}
              </button>
            ) : (
              <button className="text-button" onClick={() => setSettings(true)}>
                Configure connection <Settings2 size={12} />
              </button>
            )}
          </footer>
        </main>
      </div>
      {notice && (
        <div role="status" className="toast">
          <Radio size={17} />
          <span>{notice}</span>
          <button aria-label="Dismiss notification" onClick={() => notify("")}>
            <X size={16} />
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
            setSelected(next.source === "simulator" ? "sim-backpack" : null);
            setConfig(next);
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
                const ok = await command("/session");
                if (ok) {
                  setSelected(simulated ? "sim-backpack" : null);
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
            <p>
              God’s Eye connects a live map with persistent object observations,
              so you can see what is here and what has changed.
            </p>
            <h3>
              <MapPin size={17} /> Explore the scene
            </h3>
            <p>
              Middle-drag to orbit, Shift + middle-drag to pan, and scroll to
              zoom. Use the Orbit and Pan tools for left-button dragging. Press
              Home to reset.
            </p>
            <h3>
              <Box size={17} /> Follow an object
            </h3>
            <p>
              Select a marker or inventory row to inspect confidence, position,
              and received history. Amber indicates a detected change.
            </p>
            <h3>
              <ScanLine size={17} /> Try the demo
            </h3>
            <p>
              Leave the simulator disarmed and run the relocation demo. The
              backpack moves 1.6 meters and its old and new positions appear
              together.
            </p>
            <div className="info-note">
              <ShieldAlert size={18} />
              <p>
                Simulation is always labeled. The real backend currently cannot
                drive hardware, and unsupported actions show an error.
              </p>
            </div>
          </div>
        </Dialog>
      )}
    </div>
  );
}
