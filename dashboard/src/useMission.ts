import {
  PointCloudStore,
  POINTS_PROTOCOL,
  type CapturedPoints,
} from "./pointCloud";
import { CloudWorker } from "./cloudWorker";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  parseMessage,
  parseEventHistory,
  mapKey,
  type MapScope,
  type Message,
} from "./protocol";
import { emptyMission, reduceMessage } from "./state";
import { DirectionalSteering, type SteeringDirection } from "./steering";
import { useAutonomy } from "./useAutonomy";
import {
  initialConfig,
  updateFeedUrl,
  ManualController,
  sendCommand,
  type ConnectionConfig,
} from "./transport";

import {
  restoreControlPairing,
  rememberControlPairing,
} from "./controlPairing";

export function useMission() {
  const [cloud] = useState(() => new PointCloudStore());
  const pointWorker = useRef<CloudWorker | null>(null);
  useEffect(() => {
    const worker = new CloudWorker(
      cloud,
      () => {},
      () => setNotice("Point worker unavailable; reload to resume."),
    );
    pointWorker.current = worker;
    return () => {
      worker.dispose();
      pointWorker.current = null;
    };
  }, [cloud]);
  const ingestCaptured = useCallback(
    (points: CapturedPoints, restore = false) => {
      if (restore) pointWorker.current?.restoreCoverage();
      return (
        pointWorker.current?.ingestCaptured(points) ?? Promise.resolve(false)
      );
    },
    [],
  );
  const [config, setConfig] = useState<ConnectionConfig>(() => {
    const initial = initialConfig();
    try {
      return restoreControlPairing(initial, window.sessionStorage);
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    if (
      config.serverPaired &&
      new URL(config.apiUrl).origin !== window.location.origin
    ) {
      setConfig((current) => ({
        ...current,
        commands: false,
        serverPaired: false,
      }));
      return;
    }
    if (
      config.serverPaired ||
      new URL(config.apiUrl).origin !== window.location.origin
    )
      return;
    let active = true;
    void fetch(`${config.apiUrl}/operator/status`, { cache: "no-store" })
      .then((response) => response.json())
      .then((status) => {
        if (active && status.paired === true)
          setConfig((current) => ({
            ...current,
            commands: true,
            serverPaired: true,
          }));
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [config.apiUrl, config.serverPaired]);
  const autonomy = useAutonomy(config.apiUrl);
  useEffect(() => {
    updateFeedUrl(config);
    try {
      rememberControlPairing(config, window.sessionStorage);
    } catch {
      /* Storage blocked. */
    }
  }, [config]);
  const [mission, setMission] = useState(emptyMission);
  const [connection, setConnection] = useState("connecting");
  const [mapConfirmed, setMapConfirmed] = useState(false);
  const confirmedMap = useRef(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [stopLatched, setStopLatched] = useState(true);
  const [rescanBaseline, setRescanBaseline] = useState<string | null>(null);
  const [historyStatus, setHistoryStatus] = useState("Live events only");
  const [pending, setPending] = useState<string | null>(null);
  const [unconfirmedMotion, setUnconfirmedMotion] = useState(false);
  const [lastKnownArmed, setLastKnownArmed] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [startedAt, setStartedAt] = useState(Date.now());
  const manual = useRef<ManualController | null>(null);
  const generation = useRef(0);
  const stopEpoch = useRef(0);
  const stopLatchRef = useRef(true);
  const controlEpoch = useRef(0);
  const armSetupPending = useRef(false);
  const controlBusy = useRef<number | null>(null);
  const heldDirection = useRef<SteeringDirection | null>(null);
  const [steeringDirection, setSteeringDirection] =
    useState<SteeringDirection | null>(null);
  const directional = useRef<DirectionalSteering | null>(null);
  const motionState = useRef({ ready: false, healthy: false, yaw: 0 });
  const latchStop = useCallback((value: boolean) => {
    stopLatchRef.current = value;
    setStopLatched(value);
  }, []);
  const cancelControl = useCallback(() => {
    controlEpoch.current++;
    heldDirection.current = null;
    setSteeringDirection(null);
    directional.current?.stop();
    manual.current?.stop();
  }, []);
  const releaseSteering = useCallback(() => {
    if (heldDirection.current !== null) cancelControl();
  }, [cancelControl]);
  const activeMap = useRef<string | null>(null);
  const cloudMap = useRef<string | null>(null);
  const { source, wsUrl, apiUrl } = config;
  const receive = useCallback((messages: Message[], wire?: unknown) => {
    for (const message of messages) {
      const key = mapKey(message);
      if (key && key !== cloudMap.current) {
        cloudMap.current = key;
        pointWorker.current?.reset();
        pointWorker.current?.announce({
          version: 1,
          type: "objects",
          session_id: message.session_id,
          map_epoch: message.map_epoch,
        });
      }
      if (message.type === "points")
        pointWorker.current?.ingest(
          wire instanceof ArrayBuffer ? wire.slice(0) : message,
        );
    }
    setMission((s) =>
      messages.reduce((state, m) => reduceMessage(state, m), s),
    );
  }, []);
  const notify = useCallback((message: string) => setNotice(message), []);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    if (notice) {
      const id = setTimeout(() => setNotice(null), 6500);
      return () => clearTimeout(id);
    }
  }, [notice]);
  useEffect(() => {
    const gen = ++generation.current;
    cancelControl();
    controlBusy.current = null;
    activeMap.current = null;
    cloudMap.current = null;
    confirmedMap.current = false;
    setMapConfirmed(confirmedMap.current);
    stopEpoch.current++;
    let disposed = false,
      timer: ReturnType<typeof setTimeout> | undefined,
      socket: WebSocket | undefined,
      attempt = 0,
      legacySocket = false;
    manual.current?.stop();
    setMission(emptyMission());
    pointWorker.current?.reset();
    setStartedAt(Date.now());
    setPending(null);
    setUnconfirmedMotion(false);
    setLastKnownArmed(false);
    latchStop(true);
    const connect = () => {
      if (disposed) return;
      let opened = false;
      setConnection(attempt ? "reconnecting" : "connecting");
      try {
        socket = legacySocket
          ? new WebSocket(wsUrl)
          : new WebSocket(wsUrl, POINTS_PROTOCOL);
        socket.binaryType = "arraybuffer";
      } catch {
        setConnection("disconnected");
        setNotice("Could not open the configured WebSocket.");
        return;
      }
      socket.onopen = () => {
        if (disposed) return;
        opened = true;
        attempt = 0;
        pointWorker.current?.reconnect();
        confirmedMap.current = false;
        setMapConfirmed(false);
        stopEpoch.current++;
        cancelControl();
        latchStop(true);
        // A transport reconnect is not a new AR map. Keep historical spatial
        // memory, but require fresh identity/pose/health before resuming live use.
        // Point IDs may restart: clear only deduplication, retaining observed geometry.
        setMission((state) => ({
          ...state,
          health: null,
          healthAt: 0,
          pose: null,
          path: [],
          pointIds: [],
          detections: null,
        }));
        setConnection("connected");
      };
      socket.onmessage = (e) => {
        if (disposed || gen !== generation.current) return;
        const message = parseMessage(e.data);
        if (message) {
          const key = mapKey(message);
          if (
            activeMap.current !== null &&
            !confirmedMap.current &&
            (key === null || (key === undefined && message.type !== "health"))
          )
            // An empty restarting backend does not establish a different map.
            // Unscoped pose/path/object updates cannot establish continuity either.
            return;
          if (key !== undefined) {
            confirmedMap.current = key !== null;
            setMapConfirmed(confirmedMap.current);
          }
          if (key !== undefined && key !== activeMap.current) {
            activeMap.current = key;
            setStartedAt(Date.now());
            stopEpoch.current++;
            cancelControl();
            latchStop(true);
          }
          receive([message], e.data);
        }
      };
      socket.onerror = () => {
        if (!disposed) {
          confirmedMap.current = false;
          setMapConfirmed(false);
          setConnection("reconnecting");
        }
      };
      socket.onclose = () => {
        if (disposed) return;
        confirmedMap.current = false;
        setMapConfirmed(false);
        stopEpoch.current++;
        cancelControl();
        latchStop(true);
        // Fall back only after a failed handshake; ordinary reconnects retain
        // the negotiated dense stream.
        if (!opened) legacySocket = true;
        setConnection("reconnecting");
        timer = setTimeout(connect, Math.min(8000, 1000 * 2 ** attempt++));
      };
    };
    connect();
    return () => {
      disposed = true;
      clearTimeout(timer);
      manual.current?.stop();
      if (socket) {
        socket.onclose = null;
        socket.onmessage = null;
        socket.close();
      }
    };
  }, [source, wsUrl, apiUrl, receive, cancelControl, latchStop]);
  useEffect(() => {
    // REST permission changes revoke in-flight control authority without closing
    // the telemetry connection or erasing a scan from the same source/map.
    stopEpoch.current++;
    latchStop(true);
    cancelControl();
  }, [config.commands, cancelControl, latchStop]);
  useEffect(() => {
    setRescanBaseline(null);
    latchStop(true);
    cancelControl();
    if (
      config.source !== "external" ||
      !config.commands ||
      connection !== "connected" ||
      !mapConfirmed ||
      !mission.mapKey
    ) {
      setHistoryStatus("Live events only");
      return;
    }
    const abort = new AbortController();
    let active = true;
    const gen = generation.current;
    const key = mission.mapKey;
    setHistoryStatus("Loading saved history…");
    const timeout = setTimeout(() => abort.abort(), 4000);
    void fetch(`${config.apiUrl.replace(/\/$/, "")}/events`, {
      signal: abort.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw Error("History unavailable");
        const messages = parseEventHistory(await response.json(), key);
        if (!messages) throw Error("History does not match the active map");
        if (active && gen === generation.current) {
          setMission((state) =>
            state.mapKey === key
              ? messages.reduce((s, m) => reduceMessage(s, m), state)
              : state,
          );
          setHistoryStatus("Saved + live history");
        }
      })
      .catch(() => {
        if (active)
          setHistoryStatus("Saved history unavailable · live events only");
      })
      .finally(() => clearTimeout(timeout));
    return () => {
      active = false;
      clearTimeout(timeout);
      abort.abort();
    };
  }, [
    config,
    connection,
    mapConfirmed,
    mission.mapKey,
    cancelControl,
    latchStop,
  ]);
  const send = useCallback(
    async (
      path: string,
      body?: Record<string, unknown>,
      signal?: AbortSignal,
      standard = false,
    ) => {
      if (!config.commands && path !== "/stop")
        throw Error(
          "This feed is telemetry only. Configure a REST API to send commands.",
        );
      // Only an attempted command creates uncertainty. A telemetry-only local
      // rejection sends nothing and must not invent an outstanding motion state.
      if (path === "/arm" || path === "/stop") setUnconfirmedMotion(true);
      const gen = generation.current;
      const requestedMap = activeMap.current;
      const preparing = path === "/arm" && config.serverPaired;
      const result = await sendCommand(
        config.apiUrl,
        // Phone setup defaults to Explore; `standard` keeps Standard for one confirmed voice move.
        preparing
          ? `/arm?prepare=true${standard ? "&standard=true" : ""}`
          : path,
        body,
        preparing ? AbortSignal.timeout(18000) : signal,
        config.roverKey,
      );
      if (gen !== generation.current) return result;
      if (path === "/stop") {
        setUnconfirmedMotion(false);
        setLastKnownArmed(false);
      }
      if (path === "/session") {
        const ack = parseMessage({
          ...(result as MapScope),
          version: 1,
          type: "objects",
          objects: [],
        });
        const key = ack && mapKey(ack);
        if (activeMap.current !== requestedMap && activeMap.current !== key)
          return result;
        // /live can publish the reset snapshot before the REST reply arrives.
        if (key && activeMap.current !== key) {
          activeMap.current = key;
          confirmedMap.current = true;
          setMapConfirmed(true);
          receive([ack!]);
        }
        setStartedAt(Date.now());
      }
      return result;
    },
    [config, receive],
  );
  useEffect(() => {
    const controller = new ManualController(
      (body, signal) => send("/manual", body, signal),
      (e) => notify(e instanceof Error ? e.message : "Manual command failed."),
    );
    manual.current = controller;
    const steering = new DirectionalSteering(
      (body, signal) => send("/manual", { ...body }, signal),
      () => motionState.current,
      (e) => {
        cancelControl();
        notify(e instanceof Error ? e.message : "Steering command failed.");
      },
    );
    directional.current = steering;
    const stop = () => cancelControl();
    const hidden = () => {
      if (document.hidden) stop();
    };
    window.addEventListener("blur", stop);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      steering.stop();
      controller.stop();
      window.removeEventListener("blur", stop);
      document.removeEventListener("visibilitychange", hidden);
    };
  }, [send, notify, cancelControl]);
  const stale =
    connection !== "connected" ||
    !mission.health ||
    now - mission.healthAt > 2000;
  const trackingNormal =
    !stale &&
    mission.pose?.tracking === "normal" &&
    mission.health?.phone === "ok" &&
    mission.health.pose_age_ms !== null &&
    mission.health.pose_age_ms <= 250;
  const healthy =
    trackingNormal &&
    mission.health?.car === "ok" &&
    mission.health.detector === "ok";
  const canDrive =
    healthy &&
    !stopLatched &&
    !pending &&
    mission.health?.armed === true &&
    config.commands;
  useEffect(() => {
    if (mission.health) {
      setLastKnownArmed(mission.health.armed);
      if (mission.health.armed) setUnconfirmedMotion(false);
    }
  }, [mission.health?.armed]);
  // A reconnect can temporarily clear health. Unknown state is not disarm.
  const requiresStop =
    unconfirmedMotion ||
    lastKnownArmed ||
    !!mission.health?.armed ||
    !!pending ||
    !!autonomy?.auto_requested;
  const unexpectedStop =
    mission.health?.armed === false &&
    mission.health.stop_reason !== null &&
    !["mode_change", "Mode changed; rearm required"].includes(
      mission.health.stop_reason,
    );
  motionState.current = {
    ready:
      canDrive && !stopLatchRef.current && mission.health?.mode === "manual",
    healthy: healthy && !unexpectedStop,
    yaw: mission.pose?.yaw_rad ?? 0,
  };
  useEffect(() => {
    if (!healthy || unexpectedStop) {
      if (!armSetupPending.current) stopEpoch.current++;
      latchStop(true);
      cancelControl();
    }
  }, [healthy, unexpectedStop, cancelControl, latchStop]);
  useEffect(() => {
    if (!canDrive) manual.current?.stop();
    if (canDrive && mission.health?.mode === "manual" && steeringDirection)
      directional.current?.start(steeringDirection);
    else directional.current?.stop();
  }, [canDrive, mission.health?.mode, steeringDirection]);
  const command = useCallback(
    async (path: string, body?: Record<string, unknown>, standard = false) => {
      cancelControl();
      controlBusy.current = null;
      if (path === "/arm") armSetupPending.current = true;
      const gen = generation.current;
      if (["/stop", "/mode", "/session"].includes(path)) latchStop(true);
      if (["/stop", "/mode", "/session"].includes(path)) stopEpoch.current++;
      if (path === "/stop") setPending(null);
      const commandEpoch = stopEpoch.current;
      const requestMap = activeMap.current;
      if (path !== "/stop") setPending(path);
      try {
        const result = await send(path, body, undefined, standard);
        if (
          path === "/rescan" &&
          config.source === "external" &&
          gen === generation.current
        ) {
          const ack = result as MapScope & {
            version?: number;
            rescan_id?: string;
            baseline_objects?: number;
          };
          if (
            ack.version !== 1 ||
            !(typeof ack.rescan_id === "string" && ack.rescan_id.length > 0) ||
            !Number.isSafeInteger(ack.baseline_objects) ||
            Number(ack.baseline_objects) < 0
          )
            throw Error(
              "Rescan response was not a valid baseline acknowledgement.",
            );
          if (
            !requestMap ||
            requestMap !== activeMap.current ||
            mapKey(ack) !== requestMap ||
            commandEpoch !== stopEpoch.current
          )
            return false;
          setRescanBaseline(
            `Baseline saved · ${ack.baseline_objects} objects · watching observations`,
          );
          notify(
            `Baseline saved for ${ack.baseline_objects} objects. Watching new observations.`,
          );
        }
        if (
          path === "/arm" &&
          (gen !== generation.current || commandEpoch !== stopEpoch.current)
        ) {
          await send("/stop");
          return false;
        }
        if (gen !== generation.current) return false;
        if (path === "/arm") latchStop(false);
        if (path === "/stop") notify("Stop acknowledged. Rover disarmed.");
        return true;
      } catch (e) {
        let message = e instanceof Error ? e.message : "Command failed.";
        if (path === "/arm") {
          if (gen === generation.current) latchStop(true);
          // An error response does not prove the backend never applied Arm.
          // Use this operation's captured endpoint even after a source change.
          try {
            await send("/stop");
          } catch {
            message += " Stop could not be confirmed.";
          }
        }
        if (gen === generation.current) notify(message);
        return false;
      } finally {
        if (path === "/arm") armSetupPending.current = false;
        if (gen === generation.current && path !== "/stop") setPending(null);
      }
    },
    [send, notify, config.source, cancelControl, latchStop],
  );
  const handoff = useCallback(
    async (
      mode: "manual" | "navigate",
      action: () => Promise<unknown> | void,
      direction?: SteeringDirection,
    ) => {
      // A directional input can continue an already armed Standard session; it
      // cannot arm a stopped rover, bypass faults, or take over Explore silently.
      if (
        !canDrive ||
        stopLatchRef.current ||
        mission.health?.mode === "explore" ||
        controlBusy.current !== null
      )
        return false;
      cancelControl();
      const token = controlEpoch.current;
      const gen = generation.current;
      const epoch = stopEpoch.current;
      const valid = () =>
        token === controlEpoch.current &&
        gen === generation.current &&
        epoch === stopEpoch.current &&
        motionState.current.healthy;
      if (direction) heldDirection.current = direction;
      controlBusy.current = token;
      const switching = mission.health?.mode !== mode;
      setPending(switching ? "/mode" : mode === "navigate" ? "/goal" : null);
      try {
        if (switching) {
          latchStop(true);
          await send("/mode", { mode });
          if (!valid()) return false;
          // A Standard arm on the iPhone adapter only serves a confirmed voice move;
          // direct steering stays refused there.
          await send("/arm", undefined, undefined, mode === "manual");
          if (!valid()) {
            // The request can complete after release, Stop, or a source switch.
            // Reassert Stop against the endpoint captured by this operation.
            await send("/stop");
            return false;
          }
          latchStop(false);
        }
        if (!valid()) return false;
        await action();
        if (!valid()) {
          if (mode === "navigate") await send("/stop");
          return false;
        }
        return true;
      } catch (e) {
        if (gen === generation.current) {
          cancelControl();
          latchStop(true);
          notify(e instanceof Error ? e.message : "Control handoff failed.");
        }
        // A rejected or timed-out command must not leave a hidden navigation
        // job or a late arm running behind a failed UI operation.
        await send("/stop").catch(() => {});
        return false;
      } finally {
        if (gen === generation.current && controlBusy.current === token) {
          controlBusy.current = null;
          setPending(null);
        }
      }
    },
    [canDrive, mission.health?.mode, cancelControl, latchStop, send, notify],
  );
  const steer = useCallback(
    (direction: SteeringDirection) => {
      if (autonomy?.adapter === "iphone") {
        notify(
          "Use the phone for manual driving. Select a map destination for laptop navigation.",
        );
        return;
      }
      if (heldDirection.current === direction) return;
      if (
        heldDirection.current &&
        mission.health?.mode === "manual" &&
        canDrive
      ) {
        heldDirection.current = direction;
        setSteeringDirection(direction);
        return;
      }
      void handoff("manual", () => setSteeringDirection(direction), direction);
    },
    [handoff, canDrive, mission.health?.mode, autonomy?.adapter, notify],
  );
  const navigate = useCallback(
    (x: number, z: number) => {
      if (!Number.isFinite(x) || !Number.isFinite(z))
        return Promise.resolve(false);
      return handoff("navigate", () => send("/goal", { x, z }));
    },
    [handoff, send],
  );
  const confirmProposal = useCallback(
    (kind: string, body: Record<string, unknown>) => {
      // A destination uses the same hand-off as click-to-navigate: it needs a
      // deliberate arm first, and the backend then plans it exactly like /goal.
      if (kind === "destination")
        return handoff("navigate", () => send("/nav/confirm", body));
      // One measured move: the same hand-off into Standard, then the backend runs it once.
      if (kind === "move")
        return handoff("manual", () => send("/nav/confirm", body));
      // Exploration only selects explore mode, which stops and disarms; arming
      // to start it stays a separate deliberate click.
      stopEpoch.current++;
      latchStop(true);
      return command("/nav/confirm", body);
    },
    [handoff, send, command, latchStop],
  );
  /** A deliberate click on a move card: select Standard, then arm (with phone setup) for that move. */
  const armForMove = useCallback(async () => {
    if (mission.health?.mode === "explore") return false;
    if (
      mission.health?.mode !== "manual" &&
      !(await command("/mode", { mode: "manual" }))
    )
      return false;
    return command("/arm", undefined, true);
  }, [command, mission.health?.mode]);
  const keyboard = useRef({ steer, releaseSteering });
  keyboard.current = { steer, releaseSteering };
  useEffect(() => {
    const keys: Record<string, SteeringDirection> = {
      ArrowUp: "up",
      ArrowDown: "down",
      ArrowLeft: "left",
      ArrowRight: "right",
    };
    const ignored = (target: EventTarget | null) =>
      document.querySelector("dialog[open], .workspace-drawer") ||
      (target instanceof Element &&
        target.closest(
          "input, textarea, select, [contenteditable]:not([contenteditable='false'])",
        ));
    const down = (event: KeyboardEvent) => {
      const direction = keys[event.key];
      if (
        !direction ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        ignored(event.target)
      )
        return;
      event.preventDefault();
      if (!event.repeat) keyboard.current.steer(direction);
    };
    const up = (event: KeyboardEvent) => {
      if (keys[event.key] === heldDirection.current)
        keyboard.current.releaseSteering();
    };
    const focus = () => {
      if (ignored(document.activeElement)) keyboard.current.releaseSteering();
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    document.addEventListener("focusin", focus);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      document.removeEventListener("focusin", focus);
    };
  }, []);
  const drive = (v: number, w: number) => {
    if (canDrive && mission.health?.mode === "manual")
      manual.current?.start(v, w);
  };
  return {
    cloud,
    ingestCaptured,
    mission,
    rescanBaseline,
    historyStatus,
    config,
    autonomy,
    setConfig,
    connection,
    mapConfirmed,
    stale,
    trackingNormal,
    canDrive,
    requiresStop,
    command,
    pending,
    notice,
    notify,
    now,
    startedAt,
    drive,
    steer,
    releaseSteering,
    navigate,
    confirmProposal,
    armForMove,
    release: cancelControl,
  };
}
export type MissionController = ReturnType<typeof useMission>;
