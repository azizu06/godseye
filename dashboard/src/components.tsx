import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Backpack,
  Box,
  Check,
  ChevronRight,
  Circle,
  CircleHelp,
  Clock3,
  Coffee,
  Crosshair,
  Download,
  ExternalLink,
  Laptop,
  Leaf,
  LoaderCircle,
  Navigation,
  Power,
  Radio,
  ScanLine,
  Search,
  Settings2,
  ShieldCheck,
  Square,
  X,
} from "lucide-react";
import type { ChangeEvent, WorldObject } from "./protocol";
import type { MissionController } from "./useMission";
import {
  defaultConfig,
  validateConfig,
  type ConnectionConfig,
} from "./transport";
import { objectName } from "./Scene";

export function ObjectIcon({
  kind,
  size = 18,
}: {
  kind: string;
  size?: number;
}) {
  const Icon =
    kind === "backpack"
      ? Backpack
      : kind === "laptop"
        ? Laptop
        : kind === "potted plant"
          ? Leaf
          : kind === "bottle"
            ? Coffee
            : kind === "chair"
              ? Box
              : Circle;
  return <Icon size={size} />;
}
export const stateLabel = (state: string) =>
  ({
    present: "Present",
    last_seen: "Last seen",
    moved: "Moved",
    not_found_on_rescan: "Not found on rescan",
  })[state] ?? state;
export const timeLabel = (t: number) =>
  new Date(t * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
export function Dialog({
  title,
  children,
  onClose,
  onStop,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  onStop: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className="dialog"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="dialog-heading">
        <h2>{title}</h2>
        <button className="stop-button dialog-stop" onClick={onStop}>
          <Square size={10} fill="currentColor" /> STOP
        </button>
        <button
          className="icon-button"
          aria-label="Close dialog"
          onClick={onClose}
        >
          <X size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function Settings({
  config,
  onSave,
  onClose,
  onStop,
}: {
  config: ConnectionConfig;
  onSave: (config: ConnectionConfig) => Promise<boolean>;
  onClose: () => void;
  onStop: () => void;
}) {
  const [draft, setDraft] = useState(config),
    [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  return (
    <Dialog title="Connect your world" onClose={onClose} onStop={onStop}>
      <p className="dialog-intro">
        Choose a source for your spatial workspace.
      </p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const issue = validateConfig(draft);
          setError(issue);
          if (!issue) {
            setSaving(true);
            const changed = await onSave(draft);
            setSaving(false);
            if (changed) onClose();
            else
              setError(
                "Could not stop the previous backend. Connection unchanged; retry Stop before switching.",
              );
          }
        }}
      >
        <div className="source-options">
          <button
            type="button"
            className={draft.source === "simulator" ? "selected" : ""}
            onClick={() => setDraft({ ...draft, source: "simulator" })}
          >
            <Box size={22} />
            <strong>Local simulator</strong>
            <span>
              A complete, interactive demo.
              <br />
              No hardware connected.
            </span>
            {draft.source === "simulator" && (
              <Check className="source-check" size={16} />
            )}
          </button>
          <button
            type="button"
            className={draft.source === "external" ? "selected" : ""}
            onClick={() => setDraft({ ...draft, source: "external" })}
          >
            <Radio size={22} />
            <strong>External feed</strong>
            <span>
              Connect to your Mac backend
              <br />
              or a synthetic WebSocket.
            </span>
            {draft.source === "external" && (
              <Check className="source-check" size={16} />
            )}
          </button>
        </div>
        {draft.source === "external" && (
          <div className="connection-fields">
            <label>
              Telemetry WebSocket
              <input
                value={draft.wsUrl}
                onChange={(e) => setDraft({ ...draft, wsUrl: e.target.value })}
                placeholder="ws://localhost:8765/live"
                required
              />
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={draft.commands}
                onChange={(e) =>
                  setDraft({ ...draft, commands: e.target.checked })
                }
              />{" "}
              Enable REST commands
            </label>
            <label>
              Backend API base
              <input
                value={draft.apiUrl}
                onChange={(e) => setDraft({ ...draft, apiUrl: e.target.value })}
                placeholder="http://localhost:8765"
                required
              />
            </label>
            <small>
              Color surfaces use this API even when drive commands are off.
            </small>
            <div className="preset-row">
              <span>Quick setup</span>
              <button
                type="button"
                onClick={() =>
                  setDraft({ ...defaultConfig, source: "external" })
                }
              >
                Mac backend
              </button>
              <button
                type="button"
                onClick={() =>
                  setDraft({
                    ...draft,
                    source: "external",
                    wsUrl: "ws://localhost:8766/live",
                    commands: false,
                  })
                }
              >
                Python fake feed
              </button>
            </div>
            <p className="muted small">
              The Python fake feed supplies telemetry only. Hardware controls
              require a verified backend.
            </p>
          </div>
        )}
        {draft.source === "simulator" && (
          <div className="info-note">
            <ShieldCheck size={18} />
            <p>
              All geometry, observations, and motion are simulated locally.
              Nothing is sent to a real rover.
            </p>
          </div>
        )}
        {error && (
          <p role="alert" className="form-error">
            {error}
          </p>
        )}
        <div className="dialog-footer">
          <button type="button" className="button subtle" onClick={onClose}>
            Cancel
          </button>
          <button className="button primary" type="submit" disabled={saving}>
            {draft.source === "simulator"
              ? "Start simulator"
              : "Connect source"}
            <ArrowRight size={15} />
          </button>
        </div>
      </form>
    </Dialog>
  );
}
export function ObjectList({
  objects,
  selected,
  onSelect,
  full = false,
}: {
  objects: WorldObject[];
  selected: string | null;
  onSelect: (id: string) => void;
  full?: boolean;
}) {
  const [search, setSearch] = useState("");
  const filtered = objects.filter((o) =>
    `${o.class} ${o.id}`.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div className={`object-list ${full ? "full" : ""}`}>
      <div className="search-field">
        <Search size={15} />
        <input
          aria-label="Search objects"
          placeholder="Find an object…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <kbd>⌕</kbd>
      </div>
      {filtered.map((o, i) => (
        <button
          className={`object-row ${selected === o.id ? "selected" : ""}`}
          key={o.id}
          onClick={() => onSelect(o.id)}
        >
          <span className={`object-icon ${o.state === "moved" ? "amber" : ""}`}>
            <ObjectIcon kind={o.class} />
          </span>
          <span className="object-row-copy">
            <strong>
              {objectName(o)}{" "}
              <span className="object-number">
                {String(i + 1).padStart(2, "0")}
              </span>
            </strong>
            <small>
              {stateLabel(o.state)} <span>·</span>{" "}
              {Math.round(o.confidence * 100)}% confidence
            </small>
          </span>
          <ChevronRight size={15} />
        </button>
      ))}
      {!filtered.length && (
        <div className="empty-small">
          {objects.length
            ? "No objects match your search."
            : "Objects will appear as they are observed."}
        </div>
      )}
    </div>
  );
}
export function Inspector({
  object,
  events,
  onFocus,
  now,
}: {
  object: WorldObject | undefined;
  events: ChangeEvent[];
  onFocus: () => void;
  now: number;
}) {
  if (!object)
    return (
      <div className="inspector-empty">
        <Crosshair size={28} />
        <h3>A little more perspective</h3>
        <p>
          Select an object in the scene to explore what the rover knows about
          it.
        </p>
      </div>
    );
  const history = events.filter(
      (e) => e.object_id === object.id || e.new_object_id === object.id,
    ),
    move = history
      .filter((e) => e.kind === "moved" || e.kind === "possible_move")
      .at(-1);
  const age = Math.max(0, Math.floor(now / 1000 - object.last_seen));
  return (
    <div className="inspector">
      <div className="inspector-top">
        <span className="eyebrow">OBJECT INTELLIGENCE</span>
        <span
          className={`state-pill ${object.state === "moved" ? "amber" : ""}`}
        >
          <i />
          {stateLabel(object.state)}
        </span>
      </div>
      <div className="object-hero">
        <div
          className={`object-illustration ${object.state === "moved" ? "amber" : ""}`}
        >
          <div className="illustration-grid" />
          <ObjectIcon kind={object.class} size={68} />
          <span className="corner tl" />
          <span className="corner tr" />
          <span className="corner bl" />
          <span className="corner br" />
        </div>
        <div>
          <h2>{objectName(object)}</h2>
          <span className="object-id">{object.id}</span>
        </div>
      </div>
      <div className="confidence">
        <span>Detection confidence</span>
        <strong>
          {Math.round(object.confidence * 100)}
          <small>%</small>
        </strong>
        <div className="confidence-track">
          <i style={{ width: `${object.confidence * 100}%` }} />
        </div>
      </div>
      <div className="detail-grid">
        <div>
          <span>OBSERVATIONS</span>
          <strong>
            {object.observations}
            <small> frames</small>
          </strong>
        </div>
        <div>
          <span>LAST OBSERVED</span>
          <strong>
            {age < 60 ? `${age}s` : `${Math.floor(age / 60)}m`}
            <small> ago</small>
          </strong>
        </div>
      </div>
      <div className="position-card">
        <span className="eyebrow">ESTIMATED POSITION</span>
        <div>
          {object.position.map((v, i) => (
            <span key={i}>
              <b>{["X", "Y", "Z"][i]}</b>
              {v.toFixed(2)}
              <small>m</small>
            </span>
          ))}
        </div>
      </div>
      {move?.old_position && move.new_position && (
        <div className="movement-card">
          <div>
            <ScanLine size={17} />
            <strong>
              {move.kind === "moved"
                ? "Relocation detected"
                : "Possible relocation"}
            </strong>
          </div>
          <p>
            {move.kind === "moved"
              ? "This object was observed in a new position."
              : "Identity is uncertain. Review the spatial evidence."}
          </p>
          {move.new_object_id && (
            <p>Candidate identity: {move.new_object_id}</p>
          )}
          {move.rescan_id && <p>Rescan #{move.rescan_id}</p>}
          <div className="displacement">
            <strong>
              {move.displacement_m?.toFixed(2) ?? "—"}
              <small> m</small>
            </strong>
            <span>from its previous position</span>
          </div>
          <button onClick={onFocus}>
            View movement in scene <ArrowRight size={14} />
          </button>
        </div>
      )}
      <div className="observation-history">
        <h4>
          Observation history <span>{history.length}</span>
        </h4>
        {history
          .slice(-3)
          .reverse()
          .map((e, i) => (
            <div className="history-row" key={`${e.t}-${i}`}>
              <i className={e.kind === "moved" ? "amber" : ""} />
              <span>
                {e.kind === "new"
                  ? "First recognized"
                  : e.kind === "moved"
                    ? "Position changed"
                    : e.kind === "possible_move"
                      ? "Possible move"
                      : "Not found on rescan"}
                <small>{timeLabel(e.t)}</small>
              </span>
            </div>
          ))}
        <p>Bounded saved and live observations.</p>
      </div>
    </div>
  );
}
export function EventList({
  events,
  objects,
  onSelect,
  compact = false,
}: {
  events: ChangeEvent[];
  objects: WorldObject[];
  onSelect: (id: string) => void;
  compact?: boolean;
}) {
  const items = events.slice(compact ? -3 : -200).reverse();
  return (
    <div className={`event-list ${compact ? "compact" : ""}`}>
      {items.map((e, i) => {
        const obj = objects.find((o) => o.id === e.object_id);
        return (
          <button
            key={`${e.t}-${e.object_id}-${i}`}
            className="event-row"
            onClick={() => onSelect(e.object_id)}
          >
            <span
              className={`event-symbol ${e.kind === "moved" ? "amber" : ""}`}
            >
              {e.kind === "moved" ? (
                <ScanLine size={16} />
              ) : (
                <Crosshair size={16} />
              )}
            </span>
            <span className="event-copy">
              <strong>
                {obj ? objectName(obj) : e.object_id}{" "}
                {e.kind === "new"
                  ? "recognized"
                  : e.kind === "moved"
                    ? "moved"
                    : e.kind === "possible_move"
                      ? "may have moved"
                      : "not found on rescan"}
              </strong>
              <small>
                {e.kind === "moved"
                  ? `${e.displacement_m?.toFixed(2) ?? "—"} m displacement · same object identity`
                  : e.kind === "new"
                    ? "Added to spatial memory"
                    : "Review observation evidence"}
              </small>
            </span>
            <time>{timeLabel(e.t)}</time>
            <ChevronRight size={14} />
          </button>
        );
      })}
      {!items.length && (
        <div className="empty-small">Your room's story will appear here.</div>
      )}
    </div>
  );
}
export function OperatorControls({
  controller,
  compact = false,
}: {
  controller: MissionController;
  compact?: boolean;
}) {
  const {
    mission,
    command,
    pending,
    stale,
    canDrive,
    steer,
    releaseSteering,
    config,
  } = controller;
  const health = mission.health,
    simulation = config.source === "simulator",
    available = simulation || config.commands;
  const armed = !!health?.armed;
  const allHealthy =
    !stale &&
    health?.phone === "ok" &&
    health.car === "ok" &&
    health.detector === "ok";
  const control = (
    label: string,
    icon: ReactNode,
    direction: "up" | "down" | "left" | "right",
  ) => (
    <button
      aria-label={label}
      title={`${label} · hold to move`}
      disabled={!canDrive || health?.mode === "explore"}
      onPointerDown={(e) => {
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        steer(direction);
      }}
      onPointerUp={releaseSteering}
      onPointerCancel={releaseSteering}
      onLostPointerCapture={releaseSteering}
      onBlur={releaseSteering}
      onKeyDown={(e) => {
        if ((e.key === " " || e.key === "Enter") && !e.repeat) {
          e.preventDefault();
          steer(direction);
        }
      }}
      onKeyUp={(e) => {
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          releaseSteering();
        }
      }}
    >
      {icon}
    </button>
  );
  return (
    <section className={`operator-panel ${compact ? "compact" : ""}`}>
      <div className="section-heading">
        <div>
          <Navigation size={16} />
          <h3>Rover controls</h3>
        </div>
        <span className={`state-pill ${armed ? "" : "neutral"}`}>
          <i />
          {armed ? "Armed" : "Disarmed"}
        </span>
      </div>
      <div className="operator-content">
        <div className="mode-control">
          <span className="eyebrow">CONTROL MODE</span>
          <div className="segmented mode-tabs">
            {(["standard", "explore"] as const).map((mode) => (
              <button
                key={mode}
                disabled={!available || !!pending || stale}
                className={
                  (
                    mode === "explore"
                      ? health?.mode === "explore"
                      : health?.mode !== "explore"
                  )
                    ? "active"
                    : ""
                }
                onClick={() => {
                  if (mode === "explore" || health?.mode === "explore")
                    void command("/mode", {
                      mode: mode === "standard" ? "manual" : "explore",
                    });
                }}
              >
                {mode === "standard" ? "Standard" : "Explore"}
              </button>
            ))}
          </div>
          <p>
            {health?.mode === "explore"
              ? simulation
                ? "Simulated exploration · autonomous movement"
                : "Exploration requires backend support"
              : "Arrow keys to steer · click the map to navigate"}
          </p>
        </div>
        <div className="drive-pad">
          {control("Move forward", <ArrowUp size={17} />, "up")}
          <div>
            {control("Turn left and move", <ArrowLeft size={17} />, "left")}
            <span>
              <Navigation size={15} />
            </span>
            {control("Turn right and move", <ArrowRight size={17} />, "right")}
          </div>
          {control("Turn around and move", <ArrowDown size={17} />, "down")}
        </div>
        <div className="arm-control">
          <button
            className={`button ${armed ? "subtle" : "primary"}`}
            disabled={!available || !!pending || (!armed && !allHealthy)}
            onClick={() => void command(armed ? "/stop" : "/arm")}
          >
            {pending === "/arm" ? (
              <LoaderCircle size={15} className="spin" />
            ) : armed ? (
              <Square size={14} />
            ) : (
              <Power size={15} />
            )}{" "}
            {armed
              ? "Disarm rover"
              : simulation
                ? "Arm simulator"
                : "Arm rover"}
          </button>
          <span>
            {simulation
              ? "Virtual motion only"
              : !available
                ? "Telemetry-only source"
                : !allHealthy
                  ? "Waiting for healthy components"
                  : "Explicit arming required"}
          </span>
        </div>
      </div>
    </section>
  );
}
export function exportMission(controller: MissionController) {
  const snapshot = {
    format: "godseye-dashboard-snapshot",
    version: 1,
    exported_at: new Date().toISOString(),
    source: controller.config.source,
    scope: "Received dashboard data only; not a full backend session export",
    health: controller.mission.health,
    pose: controller.mission.pose,
    objects: controller.mission.objects,
    events: controller.mission.events,
    occupancy: controller.mission.occupancy,
    path: controller.mission.path,
    trajectory: controller.mission.trajectory,
    point_chunks: controller.mission.chunks,
  };
  const url = URL.createObjectURL(
      new Blob([JSON.stringify(snapshot, null, 2)], {
        type: "application/json",
      }),
    ),
    a = document.createElement("a");
  a.href = url;
  a.download = `godseye-${controller.config.source}-${Date.now()}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export const Icons = { Clock3, Download, ExternalLink, CircleHelp, Settings2 };
