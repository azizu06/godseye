import { bakeSurfaceColors } from "./surfaceColor";
import { SurfaceMapClient } from "./surfaceMapClient";
import { decodeSurfaceImage } from "./surfaceImage";
import { useEffect, useRef, useState } from "react";
import type { ConnectionConfig } from "./transport";
import type {
  CapturedSurface,
  SurfaceStatus,
  SurfacePatch,
} from "./surfaceTypes";
import { decodeCaptureSurface } from "./captureSurface";
import { retainSurface, surfaceMapKey } from "./surfaceStore";

type SurfaceState = {
  key: string;
  patches: CapturedSurface[];
  status: SurfaceStatus;
  persistent: SurfacePatch | null;
  cellM: number;
};
type Retention = SurfaceState & {
  client: SurfaceMapClient | null;
  lastToken: string;
  latest: number;
  capacity: boolean;
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
    persistent: null,
    cellM: 0.02,
  });
  const retained = useRef<Retention | null>(null);
  useEffect(() => {
    const bucket: Retention = {
      key,
      patches: [],
      status: "waiting",
      persistent: null,
      cellM: 0.02,
      client: null,
      lastToken: "",
      latest: -Infinity,
      capacity: false,
    };
    retained.current = bucket;
    if (config.source === "external" && map) {
      try {
        bucket.client = new SurfaceMapClient();
      } catch {
        bucket.status = "error";
      }
    }
    setState({
      key,
      patches: [],
      status: bucket.status,
      persistent: null,
      cellM: 0.02,
    });
    return () => bucket.client?.dispose();
  }, [key, config.source, map]);
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let request: AbortController | undefined;
    const bucket = retained.current;
    if (!bucket || bucket.key !== key) return;
    const publish = (status: SurfaceStatus) => {
      if (!disposed) {
        bucket.status = bucket.capacity ? "capacity" : status;
        setState({
          key,
          patches: bucket.patches,
          persistent: bucket.persistent,
          cellM: bucket.cellM,
          status: bucket.status,
        });
      }
    };
    publish(bucket.persistent ? "paused" : "waiting");
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
      let fusionTimeout: ReturnType<typeof setTimeout> | undefined;
      try {
        if (bucket.client?.busy) {
          publish("paused");
          return;
        }
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
          publish(
            bucket.persistent || bucket.patches.length ? "paused" : "waiting",
          );
          return;
        }
        if (token === bucket.lastToken) {
          publish(
            bucket.persistent || bucket.patches.length
              ? "receiving"
              : "waiting",
          );
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
        if (surface.capturedAt <= bucket.latest) {
          bucket.lastToken = token;
          publish("receiving");
          return;
        }
        if (surface.jpeg && surface.indices.length)
          surface.image = await decodeSurfaceImage(surface.jpeg, signal);
        if (disposed || signal.aborted) return;
        if (surface.image && surface.indices.length && !bucket.capacity) {
          const context = surface.image.getContext("2d");
          if (!context || !bucket.client)
            throw Error("Map integration unavailable");
          clearTimeout(timeout);
          fusionTimeout = setTimeout(() => request?.abort(), 30000);
          const pixels = context.getImageData(
            0,
            0,
            surface.image.width,
            surface.image.height,
          );
          try {
            const result = await bucket.client.add(
              bakeSurfaceColors(surface, pixels),
              signal,
            );
            if (disposed || signal.aborted) return;
            bucket.persistent = result.patch;
            bucket.cellM = result.cellM;
          } catch (error) {
            if (disposed || signal.aborted) return;
            if (error instanceof Error && /capacity/i.test(error.message))
              bucket.capacity = true;
            else throw error;
          }
        }
        bucket.patches = retainSurface(bucket.patches, surface, map);
        bucket.latest = surface.capturedAt;
        bucket.lastToken = token;
        publish(
          bucket.persistent || bucket.patches.length ? "receiving" : "waiting",
        );
      } catch {
        publish(
          bucket.persistent || bucket.patches.length ? "paused" : "error",
        );
      } finally {
        clearTimeout(timeout);
        clearTimeout(fusionTimeout);
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
    : {
        key,
        patches: [],
        persistent: null,
        cellM: 0.02,
        status: "waiting" as const,
      };
}
