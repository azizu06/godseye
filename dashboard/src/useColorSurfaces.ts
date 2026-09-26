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

// Sai viewer policy: display age is separate from motion freshness.
const SURFACE_MAX_AGE_MS = 15000;

type SurfaceState = {
  key: string;
  patches: CapturedSurface[];
  status: SurfaceStatus;
  reason: string;
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
    reason: "Waiting for map identity",
    persistent: null,
    cellM: 0.02,
  });
  const retained = useRef<Retention | null>(null);
  useEffect(() => {
    const bucket: Retention = {
      key,
      patches: [],
      status: "waiting",
      reason: "Waiting for map identity",
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
      reason: bucket.reason,
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
    const publish = (status: SurfaceStatus, reason = "Live RGB + depth") => {
      if (!disposed) {
        bucket.status = bucket.capacity ? "capacity" : status;
        bucket.reason = bucket.capacity
          ? "Map capacity reached; prior scan retained"
          : reason;
        setState({
          key,
          patches: bucket.patches,
          persistent: bucket.persistent,
          cellM: bucket.cellM,
          status: bucket.status,
          reason: bucket.reason,
        });
      }
    };
    publish(
      bucket.persistent ? "paused" : "waiting",
      !map
        ? "Waiting for map identity"
        : "Feed disconnected or map identity unconfirmed; scan retained",
    );
    if (config.source !== "external" || !map || !connected) return;
    try {
      if (!["http:", "https:"].includes(new URL(config.apiUrl).protocol))
        throw Error();
    } catch {
      publish("unavailable", "Capture API unavailable; check Backend API base");
      return;
    }
    const base = config.apiUrl.replace(/\/$/, "");
    const poll = async () => {
      request = new AbortController();
      const signal = request.signal;
      const timeout = setTimeout(() => request?.abort(), 4000);
      let fusionTimeout: ReturnType<typeof setTimeout> | undefined;
      const started = performance.now();
      try {
        if (bucket.client?.busy) {
          publish("paused", "Integrating RGB-D; prior scan retained");
          return;
        }
        const response = await fetch(`${base}/capture/status`, {
          signal,
          cache: "no-store",
        });
        if (!response.ok) {
          publish(
            "unavailable",
            "Capture API unavailable; check Backend API base",
          );
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
        const freshAge = (frame: Record<string, unknown>) =>
          typeof frame.age_ms === "number" &&
          Number.isFinite(frame.age_ms) &&
          frame.age_ms >= 0 &&
          frame.age_ms <= SURFACE_MAX_AGE_MS;
        const richTime = rich.t_capture;
        const legacyTime = record(legacy.metadata).t_capture;
        const useRich =
          richUsable &&
          typeof rich.token === "string" &&
          freshAge(rich) &&
          !(
            typeof richTime === "number" &&
            typeof legacyTime === "number" &&
            richTime < legacyTime - 0.035
          );
        // Keep rich diagnostics when no v1 fallback exists, including its expiry.
        const frame = useRich || !legacy.capture_id ? rich : legacy;
        const token = String(
          useRich ? rich.token : (frame.capture_id ?? frame.token ?? ""),
        );
        if (!token || !freshAge(frame)) {
          publish(
            bucket.persistent || bucket.patches.length ? "paused" : "waiting",
            !token
              ? "No RGB-D capture received"
              : "RGB-D capture too old or invalid; scan retained",
          );
          return;
        }
        const expiresAt = started + SURFACE_MAX_AGE_MS - Number(frame.age_ms);
        const observationReason =
          Number(frame.age_ms) > 3000
            ? `Rendering delayed RGB + depth (${(Number(frame.age_ms) / 1000).toFixed(1)} s)`
            : "Live RGB + depth";
        if (token === bucket.lastToken) {
          publish(
            bucket.persistent || bucket.patches.length
              ? "receiving"
              : "waiting",
            observationReason,
          );
          return;
        }
        const packet = await fetch(
          `${base}/capture/${useRich ? "rich/frame" : "frame"}.bin`,
          { signal, cache: "no-store" },
        );
        if (!packet.ok) {
          publish(
            "unavailable",
            "Capture API unavailable; check Backend API base",
          );
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
        if (performance.now() > expiresAt) {
          publish(
            "paused",
            "RGB-D expired during transfer; prior scan retained",
          );
          return;
        }
        // Decoder requires the capture's own normal tracking, rigid transform,
        // calibration, confidence and map identity, regardless of latest pose.
        const surface = decodeCaptureSurface(data.buffer);
        if (surfaceMapKey(surface) !== map) {
          publish("waiting", "Capture map identity does not match live feed");
          return;
        }
        if (surface.capturedAt <= bucket.latest) {
          bucket.lastToken = token;
          publish("receiving", observationReason);
          return;
        }
        if (surface.jpeg && surface.indices.length)
          surface.image = await decodeSurfaceImage(surface.jpeg, signal);
        if (disposed || signal.aborted) return;
        // Display calibrated image detail immediately; persistent integration
        // can be much slower. Do not mark the capture fused until it finishes:
        // a cancelled/failed job must remain eligible for a later retry.
        bucket.patches = retainSurface(bucket.patches, surface, map);
        publish("receiving", observationReason);
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
              {
                id: surface.id,
                positions: surface.positions,
                indices: surface.indices,
                uvs: surface.uvs,
              },
              signal,
              pixels,
              {
                sessionId: surface.sessionId,
                mapEpoch: surface.mapEpoch,
                cameraPosition: surface.cameraPosition,
                cameraForward: surface.cameraForward,
              },
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
        bucket.latest = surface.capturedAt;
        bucket.lastToken = token;
        publish(
          bucket.persistent || bucket.patches.length ? "receiving" : "waiting",
          observationReason,
        );
      } catch (error) {
        publish(
          bucket.persistent || bucket.patches.length ? "paused" : "error",
          signal.aborted
            ? "Capture request timed out; prior scan retained"
            : `Capture rejected: ${error instanceof Error ? error.message : "fetch failed"}`,
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
        reason: "Waiting for map identity",
      };
}
