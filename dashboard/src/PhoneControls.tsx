import { useEffect, useState } from "react";
import { sendCommand, type ConnectionConfig } from "./transport";

interface PhoneStatus {
  capture_running: boolean;
  capture_status: string;
  tracking: string;
  network: string;
  rover_connected: boolean;
  rover_verified: boolean;
  rover_status: string;
  control_enabled: boolean;
  control_status: string;
  peers: { id: string; name: string }[];
}

export function PhoneControls({
  config,
  onStop,
}: {
  config: ConnectionConfig;
  onStop: () => void;
}) {
  const [phone, setPhone] = useState<PhoneStatus | null>(null);
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const abort = new AbortController();
    setPhone(null);
    const poll = async () => {
      try {
        const response = await fetch(
          `${config.apiUrl.replace(/\/$/, "")}/device`,
          {
            cache: "no-store",
            signal: AbortSignal.any([abort.signal, AbortSignal.timeout(1200)]),
          },
        );
        const data = await response.json();
        if (!response.ok || data.version !== 1) throw Error("Unavailable");
        if (active) setPhone(data.connected && data.phone ? data.phone : null);
      } catch {
        if (active) setPhone(null);
      }
      if (active) timer = setTimeout(poll, 500);
    };
    void poll();
    return () => {
      active = false;
      clearTimeout(timer);
      abort.abort();
    };
  }, [config.apiUrl]);
  const available =
    !!phone &&
    config.commands &&
    !!(config.roverKey || config.serverPaired) &&
    !pending;
  const action = async (action: string, peer_id?: string) => {
    if (!available) return;
    setPending(true);
    setNotice("");
    try {
      const result = await sendCommand(
        config.apiUrl,
        "/device/action",
        { action, ...(peer_id ? { peer_id } : {}) },
        undefined,
        config.roverKey,
      );
      setNotice(String(result.message ?? "Requested"));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Phone action failed");
    } finally {
      setPending(false);
    }
  };
  return (
    <section
      className="operator-panel phone-controls"
      aria-label="Mounted phone controls"
    >
      <div className="section-heading">
        <h3>Mounted phone</h3>
        <span className="state-pill neutral">
          {phone ? "Connected" : "Offline"}
        </span>
      </div>
      {!phone ? (
        <p className="drawer-note">
          Open God’s Eye and connect Dashboard remote before mounting. Keep the
          app open.
        </p>
      ) : (
        <>
          <p className="drawer-note">
            {phone.capture_status} · {phone.tracking}
            <br />
            {phone.network}
          </p>
          <div className="preset-row">
            <button
              className="button"
              disabled={!available}
              onClick={() =>
                void action(
                  phone.capture_running ? "capture_stop" : "capture_start",
                )
              }
            >
              {phone.capture_running ? "Stop capture" : "Start capture"}
            </button>
            <button
              className="button"
              disabled={!available}
              onClick={() =>
                void action(
                  phone.rover_connected ? "rover_disconnect" : "rover_scan",
                )
              }
            >
              {phone.rover_connected
                ? "Disconnect rover"
                : "Find Bluetooth rover"}
            </button>
          </div>
          {phone.peers.map((peer) => (
            <button
              key={peer.id}
              className="button"
              disabled={!available}
              onClick={() => void action("rover_select", peer.id)}
            >
              Connect {peer.name}
            </button>
          ))}
          <p className="drawer-note">
            {phone.rover_status}
            <br />
            {phone.control_status}
          </p>
          <div className="preset-row">
            <button
              className="button"
              disabled={
                !available ||
                (!phone.control_enabled &&
                  (!phone.capture_running || !phone.rover_verified))
              }
              onClick={() =>
                void action(
                  phone.control_enabled ? "control_disable" : "control_enable",
                )
              }
            >
              {phone.control_enabled
                ? "Disable laptop control"
                : "Enable laptop control"}
            </button>
            <button className="button stop-button" onClick={onStop}>
              STOP ROVER
            </button>
          </div>
          {!config.commands || !(config.roverKey || config.serverPaired) ? (
            <p className="drawer-note">
              Enable REST commands and enter the rover pairing key in Connection
              settings.
            </p>
          ) : null}
        </>
      )}
      {notice && (
        <p role="status" className="drawer-note">
          {notice}
        </p>
      )}
    </section>
  );
}
