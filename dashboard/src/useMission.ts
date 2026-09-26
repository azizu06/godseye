import type { SurfacePatch } from "./surfaceTypes";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  parseMessage,
  parseEventHistory,
  mapKey,
  type MapScope,
  type Message,
} from "./protocol";
import { emptyMission, reduceMessage } from "./state";
import { Simulator } from "./simulator";
import { DirectionalSteering, type SteeringDirection } from "./steering";
import {
  defaultConfig,
  ManualController,
  sendCommand,
  type ConnectionConfig,
} from "./transport";

export function useMission() {
  const [config, setConfig] = useState<ConnectionConfig>(defaultConfig);
  const [mission, setMission] = useState(emptyMission);
  const [connection, setConnection] = useState("connected");
  const [notice, setNotice] = useState<string | null>(null);
  const [stopLatched, setStopLatched] = useState(true);
  const [rescanBaseline, setRescanBaseline] = useState<string | null>(null);
  const [historyStatus, setHistoryStatus] = useState("Live events only");
  const [pending, setPending] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const [startedAt, setStartedAt] = useState(Date.now());
  const simulator = useRef<Simulator | null>(null);
  const manual = useRef<ManualController | null>(null);
  const generation = useRef(0);
  const stopEpoch = useRef(0);
  const stopLatchRef = useRef(true);
  const controlEpoch = useRef(0);
  const controlBusy = useRef<number | null>(null);
  const heldDirection = useRef<SteeringDirection | null>(null);
  const [simSurface, setSimSurface] = useState<SurfacePatch | null>(null);
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
  const receive = useCallback(
    (messages: Message[]) =>
      setMission((s) =>
        messages.reduce((state, m) => reduceMessage(state, m), s),
      ),
    [],
  );
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
    stopEpoch.current++;
    let disposed = false,
      timer: ReturnType<typeof setTimeout> | undefined,
      socket: WebSocket | undefined,
      attempt = 0;
    manual.current?.stop();
    setMission(emptyMission());
    setSimSurface(null);
    setStartedAt(Date.now());
    setPending(null);
    latchStop(true);
    if (config.source === "simulator") {
      const sim = new Simulator();
      simulator.current = sim;
      setConnection("connected");
      receive(sim.snapshot(true));
      const interval = setInterval(() => {
        sim.tick(0.1);
        receive(sim.snapshot());
        setSimSurface(sim.getSurface());
      }, 100);
      return () => {
        clearInterval(interval);
        manual.current?.stop();
        simulator.current = null;
      };
    }
    simulator.current = null;
    const connect = () => {
      if (disposed) return;
      setConnection(attempt ? "reconnecting" : "connecting");
      try {
        socket = new WebSocket(config.wsUrl);
      } catch {
        setConnection("disconnected");
        setNotice("Could not open the configured WebSocket.");
        return;
      }
      socket.onopen = () => {
        if (disposed) return;
        attempt = 0;
        activeMap.current = null;
        stopEpoch.current++;
        cancelControl();
        latchStop(true);
        setMission(emptyMission());
        setSimSurface(null);
        setStartedAt(Date.now());
        setConnection("connected");
      };
      socket.onmessage = (e) => {
        if (disposed || gen !== generation.current) return;
        const message = parseMessage(e.data);
        if (message) {
          const key = mapKey(message);
          if (key !== undefined && key !== activeMap.current) {
            activeMap.current = key;
            setStartedAt(Date.now());
            stopEpoch.current++;
            cancelControl();
            latchStop(true);
          }
          receive([message]);
        }
      };
      socket.onerror = () => {
        if (!disposed) setConnection("reconnecting");
      };
      socket.onclose = () => {
        if (disposed) return;
        cancelControl();
        latchStop(true);
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
  }, [config, receive, cancelControl, latchStop]);
  useEffect(() => {
    setRescanBaseline(null);
    latchStop(true);
    cancelControl();
    if (
      config.source !== "external" ||
      !config.commands ||
      connection !== "connected" ||
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
  }, [config, connection, mission.mapKey, cancelControl, latchStop]);
  const send = useCallback(
    async (
      path: string,
      body?: Record<string, unknown>,
      signal?: AbortSignal,
    ) => {
      if (config.source === "simulator") {
        if (!simulator.current) throw Error("Simulator is starting.");
        const result = simulator.current.command(path, body);
        if (path === "/session") {
          setMission(emptyMission());
          setSimSurface(null);
          setStartedAt(Date.now());
        }
        receive(simulator.current.snapshot(path === "/session"));
        setSimSurface(simulator.current.getSurface());
        return result;
      }
      if (!config.commands)
        throw Error(
          "This feed is telemetry only. Configure a REST API to send commands.",
        );
      const gen = generation.current;
      const requestedMap = activeMap.current;
      const result = await sendCommand(config.apiUrl, path, body, signal);
      if (gen !== generation.current) return result;
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
    (config.source === "simulator" || config.commands);
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
      stopEpoch.current++;
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
    async (path: string, body?: Record<string, unknown>) => {
      cancelControl();
      controlBusy.current = null;
      const gen = generation.current;
      if (["/stop", "/mode", "/session"].includes(path)) latchStop(true);
      if (["/stop", "/mode", "/session"].includes(path)) stopEpoch.current++;
      if (path === "/stop") setPending(null);
      const commandEpoch = stopEpoch.current;
      const requestMap = activeMap.current;
      if (path !== "/stop") setPending(path);
      try {
        const result = await send(path, body);
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
        if (gen === generation.current && path !== "/stop") setPending(null);
      }
    },
    [send, notify, config.source, cancelControl, latchStop],
  );
  const trackingFault = () => {
    const sim = simulator.current;
    if (sim) {
      cancelControl();
      sim.setTracking(!sim.tracking);
      manual.current?.stop();
      receive(sim.snapshot());
    }
  };
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
          await send("/arm");
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
    [handoff, canDrive, mission.health?.mode],
  );
  const navigate = useCallback(
    (x: number, z: number) => {
      if (!Number.isFinite(x) || !Number.isFinite(z))
        return Promise.resolve(false);
      return handoff("navigate", () => send("/goal", { x, z }));
    },
    [handoff, send],
  );
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
      document.querySelector("dialog[open]") ||
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
    mission,
    simSurface,
    rescanBaseline,
    historyStatus,
    config,
    setConfig,
    connection,
    stale,
    trackingNormal,
    canDrive,
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
    release: cancelControl,
    trackingFault,
  };
}
export type MissionController = ReturnType<typeof useMission>;
