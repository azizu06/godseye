import { explorationPresentation } from "./exploration";
import { serializeSurface } from "./surfaceColor";
import type { SurfacePatch } from "./surfaceTypes";
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Backpack,
  Box,
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
  Navigation,
  Power,
  ScanLine,
  Search,
  Settings2,
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
import { objectEvidence, objectStateText } from "./objectDisplay";

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
        Connect the backend receiving your phone’s observations.
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
          {draft.commands && (
            <label>
              Rover pairing key
              <input
                type="password"
                autoComplete="off"
                value={draft.roverKey ?? ""}
                onChange={(e) =>
                  setDraft({ ...draft, roverKey: e.target.value.trim() })
                }
                placeholder="Required for the iPhone rover adapter"
              />
              <small>
                Remembered in this tab across reloads. Disable REST commands to
                forget it. Use the same key on the phone.
              </small>
            </label>
          )}
          <div className="preset-row">
            <span>Quick setup</span>
            <button
              type="button"
              onClick={() => setDraft({ ...defaultConfig })}
            >
              Mac backend
            </button>
          </div>
          <p className="muted small">
            The workspace waits for backend observations. No scene data is
            generated locally.
          </p>
        </div>
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
            Connect source
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
  now = Date.now(),
}: {
  objects: WorldObject[];
  selected: string | null;
  onSelect: (id: string) => void;
  full?: boolean;
  now?: number;
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
              {objectStateText(o, now)} <span>·</span>{" "}
              {Math.round(o.confidence * 100)}% · {o.observations}{" "}
              {o.observations === 1 ? "frame" : "frames"}
              {objectEvidence(o) === "weak" && (
                <span className="evidence-note" data-testid="weak-evidence">
                  {" "}
                  · low evidence, hidden in 3D
                </span>
              )}
              {objectEvidence(o) === "possible_person" && (
                <span className="evidence-note" data-testid="possible-person">
                  {" "}
                  · possible person, kept in 3D
                </span>
              )}
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
  onApproach,
}: {
  object: WorldObject | undefined;
  events: ChangeEvent[];
  onFocus: () => void;
  now: number;
  /** Suggest a walking approach to this person; visualization only. */
  onApproach?: () => void;
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
          {objectStateText(object, now)}
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
      {object.class === "person" && onApproach && (
        <button className="button subtle" onClick={onApproach}>
          Suggest approach route <ArrowRight size={14} />
        </button>
      )}
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
    autonomy,
  } = controller;
  const health = mission.health,
    available = config.commands;
  const armed = !!health?.armed;
  const canStop = controller.requiresStop;
  const physical = autonomy?.adapter === "iphone";
  const exploration = explorationPresentation(health, stale, mission.mapKey);
  const allHealthy =
    !stale &&
    health?.phone === "ok" &&
    health.car === "ok" &&
    health.detector === "ok" &&
    (!physical ||
      (autonomy.ready && health.mode !== "manual" && !!config.roverKey));
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
          {!health ? "Waiting for status" : armed ? "Armed" : "Disarmed"}
        </span>
      </div>
      {!compact && autonomy && (
        <div className="drawer-note" aria-label="Autonomy readiness">
          <strong>
            {physical
              ? "iPhone · Bluetooth rover"
              : "Rover motion is not connected"}
          </strong>
          {autonomy.profile === "prototype" && (
            <p>
              <strong>Uncalibrated prototype</strong>
              <br />
              Estimated geometry; motor power matches the phone manual default.
              Keep the rover in an open area and use Stop if it turns the wrong
              way.
            </p>
          )}
          <p>
            {autonomy.ready ? "Ready for explicit arming." : "Before driving:"}
          </p>
          {!autonomy.ready && (
            <ul>
              {autonomy.blockers.map((reason) => (
                <li key={reason}>{reason.replaceAll("_", " ")}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {exploration && (
        <div
          className={`exploration-status ${exploration.tone}`}
          role="status"
          aria-label="Explore scan status"
          title={exploration.detail}
        >
          <span>{exploration.title}</span>
          {!compact && (
            <>
              <p>{exploration.detail}</p>
              {exploration.progress && <small>{exploration.progress}</small>}
            </>
          )}
        </div>
      )}
      <div className="operator-content">
        <div className="mode-control">
          <span className="eyebrow">CONTROL MODE</span>
          <div className="segmented mode-tabs">
            {(physical
              ? (["navigate", "explore"] as const)
              : (["standard", "explore"] as const)
            ).map((mode) => (
              <button
                key={mode}
                aria-pressed={
                  mode === "standard"
                    ? !!health && health.mode !== "explore"
                    : health?.mode === mode
                }
                disabled={!available || !!pending || stale}
                className={
                  (
                    mode === "standard"
                      ? !!health && health.mode !== "explore"
                      : health?.mode === mode
                  )
                    ? "active"
                    : ""
                }
                onClick={() => {
                  if (mode !== "standard" || health?.mode === "explore")
                    void command("/mode", {
                      mode: mode === "standard" ? "manual" : mode,
                    });
                }}
              >
                {mode === "standard"
                  ? "Standard"
                  : mode === "navigate"
                    ? "Navigate"
                    : "Explore"}
              </button>
            ))}
          </div>
          <p>
            {health?.mode === "explore"
              ? health.exploration
                ? config.commands
                  ? "Exploration follows backend scan planning. Stop remains available."
                  : "Enable REST commands to control exploration."
                : "Exploration requires backend support"
              : physical
                ? "Select Navigate, arm, then click a mapped destination. Manual driving stays on the phone."
                : "Arrow keys to steer · click the map to navigate"}
          </p>
        </div>
        {!physical && (
          <div className="drive-pad">
            {control("Move forward", <ArrowUp size={17} />, "up")}
            <div>
              {control("Turn left and move", <ArrowLeft size={17} />, "left")}
              <span>
                <Navigation size={15} />
              </span>
              {control(
                "Turn right and move",
                <ArrowRight size={17} />,
                "right",
              )}
            </div>
            {control("Turn around and move", <ArrowDown size={17} />, "down")}
          </div>
        )}
        <div className="arm-control">
          <button
            className={`button ${canStop ? "stop-button" : "primary"}`}
            aria-label={canStop ? "STOP ROVER" : "Arm rover"}
            title={canStop ? "Stop rover" : "Arm rover"}
            disabled={
              !canStop &&
              (!available ||
                !!pending ||
                (physical ? !config.roverKey : !allHealthy))
            }
            onClick={() => void command(canStop ? "/stop" : "/arm")}
          >
            {canStop ? (
              <Square size={12} fill="currentColor" />
            ) : (
              <Power size={13} />
            )}
            {canStop ? "Stop" : compact ? "Arm" : "Arm rover"}
          </button>
          <span>
            {!available
              ? "Telemetry-only source"
              : physical
                ? !config.roverKey
                  ? "Enter the rover pairing key in Connection settings"
                  : "Click Arm to check current readiness and start"
                : !allHealthy
                  ? "Waiting for healthy components"
                  : "Explicit arming required"}
          </span>
        </div>
      </div>
    </section>
  );
}
export function exportMission(
  controller: MissionController,
  surface: SurfacePatch | null = null,
  cellM: number | null = null,
) {
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
    point_chunks: controller.mission.chunks.map((chunk) => ({
      ...chunk,
      positions: Array.from(chunk.positions),
      colors: Array.from(chunk.colors),
    })),
    colored_reconstruction: serializeSurface(surface, cellM),
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
