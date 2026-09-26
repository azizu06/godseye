import { useEffect, useRef, useState } from "react";
import { mapKey } from "./protocol";
import type { ChangeEvent } from "./protocol";
import type { ConnectionConfig } from "./transport";

export function playbackUrl(
  data: unknown,
  apiUrl: string,
  scope: string | null,
  eventId: string,
): string | null {
  if (!data || typeof data !== "object") return null;
  const value = data as Record<string, unknown>;
  if (
    value.version !== 1 ||
    value.status !== "ready" ||
    value.event_id !== eventId ||
    mapKey(value) !== scope ||
    typeof value.audio_url !== "string" ||
    !/^\/audio\/[a-f0-9]{64}\.wav$/.test(value.audio_url) ||
    typeof value.duration_s !== "number" ||
    !Number.isFinite(value.duration_s) ||
    value.duration_s <= 0 ||
    value.duration_s > 12
  )
    return null;
  const base = new URL(apiUrl);
  if (!["http:", "https:"].includes(base.protocol)) return null;
  return new URL(value.audio_url, base).href;
}

export function SpokenEvent({
  config,
  scope,
  events,
}: {
  config: ConnectionConfig;
  scope: string | null;
  events: ChangeEvent[];
}) {
  const [selected, setSelected] = useState("");
  const [url, setUrl] = useState<string | null>(null);
  const [status, setStatus] = useState("Audio is prepared on request.");
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const player = useRef<HTMLAudioElement | null>(null);
  const enabled =
    config.source === "external" && config.commands && scope !== null;
  useEffect(() => {
    request.current?.abort();
    player.current?.pause();
    setSelected("");
    setUrl(null);
    setBusy(false);
    setStatus("Audio is prepared on request.");
    return () => {
      request.current?.abort();
      player.current?.pause();
    };
  }, [config.source, config.apiUrl, config.commands, scope]);

  async function prepare() {
    if (!enabled || !selected || busy) return;
    const abort = new AbortController();
    request.current = abort;
    setBusy(true);
    setUrl(null);
    setStatus("Preparing audio…");
    try {
      const response = await fetch(
        `${config.apiUrl.replace(/\/$/, "")}/events/${encodeURIComponent(selected)}/audio`,
        { method: "POST", signal: abort.signal },
      );
      if (!response.ok) throw new Error("unavailable");
      const data: unknown = await response.json();
      if (abort.signal.aborted) return;
      const audioUrl = playbackUrl(data, config.apiUrl, scope, selected);
      setUrl(audioUrl);
      setStatus(
        audioUrl
          ? "Audio ready. Press play."
          : "Audio unavailable. Review the event evidence.",
      );
    } catch {
      if (!abort.signal.aborted)
        setStatus("Audio unavailable. Review the event evidence.");
    } finally {
      if (!abort.signal.aborted) setBusy(false);
    }
  }
  return (
    <div className="drawer-note">
      <label>
        Spoken change event{" "}
        <select
          aria-label="Spoken change event"
          value={selected}
          disabled={!enabled || busy}
          onChange={(e) => {
            player.current?.pause();
            setSelected(e.target.value);
            setUrl(null);
          }}
        >
          <option value="">Select a saved event</option>
          {events
            .filter((e) => e.id)
            .slice(-200)
            .reverse()
            .map((e) => (
              <option key={e.id} value={e.id}>
                {e.kind.replace("_", " ")} · {e.object_id.slice(0, 8)}
              </option>
            ))}
        </select>
      </label>
      <button
        disabled={!enabled || !selected || busy}
        onClick={() => void prepare()}
      >
        Prepare audio
      </button>
      <p role="status">
        {enabled
          ? status
          : "Spoken events require an external backend with API actions enabled."}
      </p>
      {url && (
        <audio
          ref={player}
          controls
          preload="none"
          src={url}
          aria-label="Change event audio"
          onError={() => {
            setUrl(null);
            setStatus("Playback unavailable.");
          }}
        />
      )}
    </div>
  );
}
