import { decodeSurfaceImage } from "./surfaceImage";
import { useEffect, useState } from "react";
import type { ConnectionConfig } from "./transport";
import type { CapturedSurface, SurfaceStatus } from "./surfaceTypes";
import { decodeCaptureSurface } from "./captureSurface";
import { retainSurface, surfaceMapKey } from "./surfaceStore";

type SurfaceState = {
  key: string;
  patches: CapturedSurface[];
  status: SurfaceStatus;
};
const record = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};
/** One request at a time, with cancellation and identity checks at both reads. */
export function useColorSurfaces(
  config: ConnectionConfig,
  map: string | null,
  connected: boolean,
) {
  const key = JSON.stringify([config.source, config.wsUrl, config.apiUrl, map]);
  const [state, setState] = useState<SurfaceState>({
    key,
    patches: [],
    status: "waiting",
  });
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let request: AbortController | undefined;
    let lastToken = "";
    let patches: CapturedSurface[] = [];
    const publish = (status: SurfaceStatus) => {
      if (!disposed) setState({ key, patches, status });
    };
    publish("waiting");
    if (config.source !== "external" || !map || !connected) return;
    try {
      if (!["http:", "https:"].includes(new URL(config.apiUrl).protocol))
        throw Error();
    } catch {
      publish("unavailable");
      return;
    }
    const base = config.apiUrl.replace(/\/$/, "");
    const poll = async () => {
      request = new AbortController();
      const signal = request.signal;
      const timeout = setTimeout(() => request?.abort(), 4000);
      try {
        const response = await fetch(`${base}/capture/status`, {
          signal,
          cache: "no-store",
        });
        if (!response.ok) {
          publish("unavailable");
          return;
        }
        const status = record(await response.json());
        const rich = record(record(record(status.rich).packets).frame);
        const legacy = record(status.frame);
        const sections = Array.isArray(rich.sections)
          ? rich.sections.map((s) => record(s).name)
          : [];
        const richUsable = ["rgb", "raw_depth", "raw_confidence"].every(
          (name) => sections.includes(name),
        );
        const richFresh =
          richUsable &&
          typeof rich.token === "string" &&
          typeof rich.age_ms === "number" &&
          rich.age_ms <= 3000;
        const frame = richFresh ? rich : legacy;
        const token = String(
          richFresh ? rich.token : (legacy.capture_id ?? ""),
        );
        if (
          !token ||
          typeof frame.age_ms !== "number" ||
          frame.age_ms > 3000 ||
          status.tracking !== "normal"
        ) {
          publish(patches.length ? "paused" : "waiting");
          return;
        }
        if (token === lastToken) {
          publish(patches.length ? "receiving" : "waiting");
          return;
        }
        const packet = await fetch(
          `${base}/capture/${richFresh ? "rich/frame" : "frame"}.bin`,
          { signal, cache: "no-store" },
        );
        if (!packet.ok) {
          publish("unavailable");
          return;
        }
        const size = Number(packet.headers.get("Content-Length"));
        if (size > 32 * 1024 * 1024) throw Error("Capture too large");
        // Bound streamed bodies too: Content-Length is not guaranteed.
        const reader = packet.body?.getReader();
        if (!reader) throw Error("Empty capture");
        const chunks: Uint8Array[] = [];
        let total = 0;
        try {
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            total += value.byteLength;
            if (total > 32 * 1024 * 1024) throw Error("Capture too large");
            chunks.push(value);
          }
        } finally {
          await reader.cancel().catch(() => {});
        }
        const data = new Uint8Array(total);
        let offset = 0;
        for (const chunk of chunks) {
          data.set(chunk, offset);
          offset += chunk.length;
        }
        if (disposed || signal.aborted) return;
        const surface = decodeCaptureSurface(data.buffer);
        if (surfaceMapKey(surface) !== map) {
          publish("waiting");
          return;
        }
        if (surface.jpeg && surface.indices.length)
          surface.image = await decodeSurfaceImage(surface.jpeg, signal);
        if (disposed || signal.aborted) return;
        patches = retainSurface(patches, surface, map);
        lastToken = token;
        publish(patches.length ? "receiving" : "waiting");
      } catch {
        publish(patches.length ? "paused" : "error");
      } finally {
        clearTimeout(timeout);
        if (!disposed) timer = setTimeout(() => void poll(), 500);
      }
    };
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      request?.abort();
    };
  }, [key, config.source, config.apiUrl, map, connected]);
  return state.key === key
    ? state
    : { key, patches: [], status: "waiting" as const };
}
