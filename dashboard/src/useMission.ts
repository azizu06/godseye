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
import {
  clearMissionPeople,
  emptyMission,
  reconnectMission,
  reduceMessage,
} from "./state";
import type { PersonClearance } from "./personMemory";
import type { SteeringDirection } from "./steering";
import { defaultManualCapabilities, joystickVector } from "./joystick";
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
  const [manualStopBarrier, setManualStopBarrier] = useState(false);
  const manualStopRef = useRef(false);
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
  const exploreCancelEpoch = useRef(0);
  const armOwners = useRef(new Map<string, object>());
  const stopLatchRef = useRef(true);
  const controlEpoch = useRef(0);
  const armSetupPending = useRef(false);
  const controlBusy = useRef<number | null>(null);
  const heldDirection = useRef<SteeringDirection | null>(null);
  const manualGesture = useRef<{
    token: number;
    vector: [number, number];
    ready: boolean;
  } | null>(null);
  const manualGeneration = useRef<number | undefined>(undefined);
  const manualAuthority = useRef<number | undefined>(undefined);
  const manualRelease = useRef<Promise<unknown>>(Promise.resolve());
  const motionState = useRef({ ready: false, healthy: false, yaw: 0 });
  const latchStop = useCallback((value: boolean) => {
    stopLatchRef.current = value;
    setStopLatched(value);
  }, []);
  const cancelControl = useCallback(() => {
    controlEpoch.current++;
    heldDirection.current = null;
    manual.current?.stop();
    manualGesture.current = null;
    manualGeneration.current = undefined;
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
  const clearPeople = useCallback(
    (map: string, cleared: PersonClearance[]) =>
      setMission((state) => clearMissionPeople(state, map, cleared)),
    [],
  );
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
    armSetupPending.current = false;
    manualStopRef.current = false;
    setManualStopBarrier(false);
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
        manualAuthority.current = undefined;
        manualRelease.current = Promise.resolve();
        attempt = 0;
        pointWorker.current?.reconnect();
        confirmedMap.current = false;
        setMapConfirmed(false);
        stopEpoch.current++;
        cancelControl();
        latchStop(true);
        setMission(reconnectMission);
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
            manualAuthority.current = undefined;
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
    exploreCancelEpoch.current++;
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
          : path === "/arm" && standard
            ? "/arm?standard=true"
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
      (body, signal) => {
        const requestGeneration = generation.current;
        const requestEpoch = stopEpoch.current;
        const request = send(
          "/manual",
          {
            ...body,
            ...(manualGeneration.current === undefined
              ? {}
              : { expected_generation: manualGeneration.current }),
          },
          signal,
        ).then((result) => {
          if (
            requestGeneration === generation.current &&
            requestEpoch === stopEpoch.current &&
            Number.isSafeInteger(result.motion_generation)
          )
            manualAuthority.current = Math.max(
              manualAuthority.current ?? -1,
              Number(result.motion_generation),
            );
          return result;
        });
        if (body.release) manualRelease.current = request.catch(() => {});
        return request;
      },
      (e) => {
        cancelControl();
        notify(e instanceof Error ? e.message : "Manual command failed.");
      },
    );
    manual.current = controller;
    manualAuthority.current = undefined;
    const stop = () => cancelControl();
    const hidden = () => {
      if (document.hidden) stop();
    };
    window.addEventListener("blur", stop);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      cancelControl();
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
    mission.health.pose_age_ms <=
      (autonomy?.profile === "prototype" ? 1000 : 250);
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
    if (!canDrive) cancelControl();
  }, [canDrive, cancelControl]);
  const manualCapabilities =
    autonomy?.adapter === "iphone"
      ? (autonomy.manual_control ?? undefined)
      : defaultManualCapabilities;
  const canJoystick =
    healthy &&
    !pending &&
    !manualStopBarrier &&
    mission.health?.armed === true &&
    config.commands &&
    !!manualCapabilities &&
    mission.health?.motion_generation !== undefined &&
    (autonomy?.adapter !== "iphone" ||
      (!!(config.roverKey || config.serverPaired) &&
        mission.health?.motion_generation !== undefined));
  const releaseDrive = useCallback((token: number | null) => {
    if (token === null || manualGesture.current?.token !== token) return;
    manual.current?.stop();
    manualGesture.current = null;
    manualGeneration.current = undefined;
  }, []);
  const beginDrive = useCallback(
    (v: number, w: number): number | null => {
      if (!canJoystick || manualStopRef.current) return null;
      cancelControl();
      const token = controlEpoch.current;
      const gen = generation.current;
      const epoch = stopEpoch.current;
      const gesture = {
        token,
        vector: [v, w] as [number, number],
        ready: false,
      };
      manualGesture.current = gesture;
      const previous = manualRelease.current;
      const ready = async () => {
        try {
          await previous;
          if (
            manualStopRef.current ||
            manualGesture.current !== gesture ||
            gen !== generation.current ||
            epoch !== stopEpoch.current
          )
            return false;
          const result = await send("/manual", {
            v_mps: 0,
            yaw_rate_rps: 0,
            takeover: true,
            expected_generation: Math.max(
              mission.health?.motion_generation ?? -1,
              manualAuthority.current ?? -1,
            ),
          });
          if (gen !== generation.current || epoch !== stopEpoch.current)
            return false;
          if (Number.isSafeInteger(result.motion_generation))
            manualAuthority.current = Math.max(
              manualAuthority.current ?? -1,
              Number(result.motion_generation),
            );
          if (
            manualGesture.current !== gesture ||
            epoch !== stopEpoch.current ||
            token !== controlEpoch.current
          )
            return false;
          if (!Number.isSafeInteger(result.motion_generation))
            throw Error("Backend did not confirm manual control");
          manualGeneration.current = manualAuthority.current = Number(
            result.motion_generation,
          );
          latchStop(false);
          gesture.ready = true;
          manual.current?.start(...gesture.vector);
          return true;
        } catch (error) {
          if (manualGesture.current === gesture) {
            releaseDrive(token);
            notify(
              error instanceof Error
                ? error.message
                : "Manual control unavailable",
            );
          }
          return false;
        }
      };
      manualRelease.current = ready();
      return token;
    },
    [
      canJoystick,
      cancelControl,
      send,
      mission.health?.motion_generation,
      releaseDrive,
      notify,
    ],
  );
  const drive = useCallback(
    (token: number | null, v: number, w: number) => {
      const gesture = manualGesture.current;
      if (
        !gesture ||
        gesture.token !== token ||
        !canJoystick ||
        manualStopRef.current
      )
        return;
      gesture.vector = [v, w];
      if (gesture.ready) manual.current?.start(v, w);
    },
    [canJoystick, releaseDrive],
  );
  const command = useCallback(
    async (path: string, body?: Record<string, unknown>, standard = false) => {
      cancelControl();
      controlBusy.current = null;
      if (path === "/arm") armSetupPending.current = true;
      if (path === "/stop") {
        manualStopRef.current = true;
        setManualStopBarrier(true);
      }
      const gen = generation.current;
      if (["/stop", "/mode", "/session"].includes(path)) latchStop(true);
      if (["/stop", "/mode", "/session"].includes(path)) {
        stopEpoch.current++;
        exploreCancelEpoch.current++;
      }
      if (path === "/stop") setPending(null);
      const commandEpoch = stopEpoch.current;
      const exploreEpoch = exploreCancelEpoch.current;
      const endpoint = config.apiUrl.replace(/\/$/, "");
      const armOwner = path === "/arm" ? {} : null;
      if (armOwner) armOwners.current.set(endpoint, armOwner);
      const supersededArm = () =>
        armOwner !== null && armOwners.current.get(endpoint) !== armOwner;
      const exploreStillRequested = () =>
        gen === generation.current &&
        exploreEpoch === exploreCancelEpoch.current;
      const knownExploreRequest =
        !standard &&
        autonomy?.adapter === "iphone" &&
        autonomy.profile === "prototype" &&
        (autonomy.auto_requested === true ||
          mission.health?.mode === "explore" ||
          (config.serverPaired && mission.health?.mode === "manual"));
      // A retired source still needs its own cleanup, without changing the
      // current source's UI state. A newer arm on that endpoint supersedes it.
      const stopThisArm = () =>
        gen === generation.current
          ? send("/stop")
          : sendCommand(
              config.apiUrl,
              "/stop",
              undefined,
              undefined,
              config.roverKey,
            );
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
        if (path === "/arm" && supersededArm()) return false;
        if (
          path === "/arm" &&
          (gen !== generation.current ||
            (knownExploreRequest
              ? !exploreStillRequested()
              : commandEpoch !== stopEpoch.current))
        ) {
          await stopThisArm();
          return false;
        }
        if (gen !== generation.current) return false;
        if (path === "/arm") {
          latchStop(false);
          manualStopRef.current = false;
          setManualStopBarrier(false);
        }
        if (path === "/stop") notify("Stop acknowledged. Rover disarmed.");
        return true;
      } catch (e) {
        let message = e instanceof Error ? e.message : "Command failed.";
        if (path === "/arm") {
          if (supersededArm()) return false;
          if (gen === generation.current) latchStop(true);
          // A prototype Explore request can remain selected while its motors
          // wait for readiness. Confirm that intent on this request's backend
          // before cleanup; a later Stop, source change or mode choice wins.
          if (!standard && config.commands && exploreStillRequested()) {
            try {
              const response = await fetch(
                `${config.apiUrl.replace(/\/$/, "")}/autonomy`,
                {
                  cache: "no-store",
                  signal: AbortSignal.timeout(1500),
                },
              );
              if (!response.ok) throw Error("Explore status unavailable");
              const status = await response.json();
              if (
                status.version === 1 &&
                status.adapter === "iphone" &&
                status.profile === "prototype" &&
                status.mode === "explore" &&
                status.auto_requested === true &&
                exploreStillRequested() &&
                !supersededArm()
              ) {
                notify(
                  "Explore is still on. Waiting for the phone, rover, and map to recover.",
                );
                return true;
              }
            } catch {
              if (
                knownExploreRequest &&
                exploreStillRequested() &&
                !supersededArm()
              ) {
                notify(
                  "Explore request is waiting for confirmation. Use Stop to cancel.",
                );
                return true;
              }
            }
          }
          if (supersededArm()) return false;
          // An error response does not prove the backend never applied Arm.
          // Use this operation's captured endpoint even after a source change.
          try {
            await stopThisArm();
          } catch {
            message += " Stop could not be confirmed.";
          }
        }
        if (gen === generation.current) notify(message);
        return false;
      } finally {
        if (gen === generation.current && !supersededArm()) {
          if (path === "/arm") armSetupPending.current = false;
          if (path !== "/stop") setPending(null);
        }
        if (armOwner && !supersededArm()) armOwners.current.delete(endpoint);
      }
    },
    [
      send,
      notify,
      config.source,
      config.apiUrl,
      config.commands,
      config.serverPaired,
      config.roverKey,
      autonomy,
      mission.health?.mode,
      cancelControl,
      latchStop,
    ],
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
          // A mode handoff preserves the operator’s existing armed intent.
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
      if (heldDirection.current === direction || !manualCapabilities) return;
      const axes = {
        up: [0, -1],
        down: [0, 1],
        left: [-1, 0],
        right: [1, 0],
      } as const;
      const [x, y] = axes[direction];
      const value = joystickVector(x, y, manualCapabilities);
      void beginDrive(value.v, value.w);
      heldDirection.current = direction;
    },
    [beginDrive, manualCapabilities],
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
    clearPeople,
    now,
    startedAt,
    drive,
    beginDrive,
    releaseDrive,
    canJoystick,
    manualCapabilities,
    steer,
    releaseSteering,
    navigate,
    confirmProposal,
    armForMove,
    release: cancelControl,
  };
}
export type MissionController = ReturnType<typeof useMission>;
