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
    activeMap.current = null;
    stopEpoch.current++;
    let disposed = false,
      timer: ReturnType<typeof setTimeout> | undefined,
      socket: WebSocket | undefined,
      attempt = 0;
    manual.current?.stop();
    setMission(emptyMission());
    setStartedAt(Date.now());
    setPending(null);
    setStopLatched(true);
    if (config.source === "simulator") {
      const sim = new Simulator();
      simulator.current = sim;
      setConnection("connected");
      receive(sim.snapshot(true));
      const interval = setInterval(() => {
        sim.tick(0.1);
        receive(sim.snapshot());
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
        setMission(emptyMission());
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
            manual.current?.stop();
            setStopLatched(true);
          }
          receive([message]);
        }
      };
      socket.onerror = () => {
        if (!disposed) setConnection("reconnecting");
      };
      socket.onclose = () => {
        if (disposed) return;
        manual.current?.stop();
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
  }, [config, receive]);
  useEffect(() => {
    setRescanBaseline(null);
    setStopLatched(true);
    manual.current?.stop();
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
  }, [config, connection, mission.mapKey]);
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
          setStartedAt(Date.now());
        }
        receive(simulator.current.snapshot(path === "/session"));
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
    const stop = () => controller.stop();
    const hidden = () => {
      if (document.hidden) stop();
    };
    window.addEventListener("blur", stop);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      controller.stop();
      window.removeEventListener("blur", stop);
      document.removeEventListener("visibilitychange", hidden);
    };
  }, [send, notify]);
  const stale =
    connection !== "connected" ||
    !mission.health ||
    now - mission.healthAt > 2000;
  const canDrive =
    !stale &&
    !stopLatched &&
    !pending &&
    mission.health?.armed === true &&
    (config.source === "simulator" || config.commands);
  useEffect(() => {
    if (!canDrive) manual.current?.stop();
  }, [canDrive]);
  const command = useCallback(
    async (path: string, body?: Record<string, unknown>) => {
      if (path !== "/arm") manual.current?.stop();
      const gen = generation.current;
      if (["/stop", "/mode", "/session"].includes(path)) setStopLatched(true);
      if (path === "/stop") stopEpoch.current++;
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
        if (path === "/arm") setStopLatched(false);
        if (path === "/stop") notify("Stop acknowledged. Rover disarmed.");
        return true;
      } catch (e) {
        if (gen === generation.current)
          notify(e instanceof Error ? e.message : "Command failed.");
        return false;
      } finally {
        if (gen === generation.current && path !== "/stop") setPending(null);
      }
    },
    [send, notify, config.source],
  );
  const trackingFault = () => {
    const sim = simulator.current;
    if (sim) {
      sim.setTracking(!sim.tracking);
      manual.current?.stop();
      receive(sim.snapshot());
    }
  };
  const drive = (v: number, w: number) => {
    if (canDrive && mission.health?.mode === "manual")
      manual.current?.start(v, w);
  };
  return {
    mission,
    rescanBaseline,
    historyStatus,
    config,
    setConfig,
    connection,
    stale,
    canDrive,
    command,
    pending,
    notice,
    notify,
    now,
    startedAt,
    drive,
    release: () => manual.current?.stop(),
    trackingFault,
  };
}
export type MissionController = ReturnType<typeof useMission>;
