import { useEffect, useRef, useState } from "react";
import { SurfaceMapClient } from "./surfaceMapClient";
import { SurfaceFeed, SURFACE_MAX_AGE_MS } from "./surfaceFeed";
import type { ConnectionConfig } from "./transport";
import type {
  CapturedSurface,
  SurfaceStatus,
  SurfacePatch,
} from "./surfaceTypes";
import type { CapturedPoints } from "./pointCloud";
import { retainSurface, surfaceMapKey } from "./surfaceStore";

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
  latest: number;
  capacity: boolean;
  coverageEpoch: number;
  cleanupFailed: boolean;
};
const closeImage = (patch: SurfacePatch) => {
  if (patch.image && "close" in patch.image) patch.image.close();
};
/** Single-flight fetching and worker fusion; disconnects retain confirmed map data. */
export function useColorSurfaces(
  config: ConnectionConfig,
  map: string | null,
  connected: boolean,
  ingestCaptured: (
    points: CapturedPoints,
    restore?: boolean,
  ) => Promise<boolean>,
) {
  const key = JSON.stringify([config.source, config.wsUrl, config.apiUrl, map]);
  const blank = (): SurfaceState => ({
    key,
    patches: [],
    status: "waiting",
    reason: "Waiting for map identity",
    persistent: null,
    cellM: 0,
  });
  const [state, setState] = useState<SurfaceState>(blank);
  const retained = useRef<Retention | null>(null);
  useEffect(() => {
    const bucket: Retention = {
      ...blank(),
      client: null,
      latest: -Infinity,
      capacity: false,
      coverageEpoch: 0,
      cleanupFailed: false,
    };
    retained.current = bucket;
    if (config.source === "external" && map) {
      try {
        bucket.client = new SurfaceMapClient();
      } catch {
        bucket.status = "error";
        bucket.reason = "Map worker unavailable";
      }
    }
    setState({ ...bucket });
    return () => {
      bucket.client?.dispose();
      bucket.patches.forEach(closeImage);
    };
  }, [key, config.source, map]);
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let request: AbortController | undefined;
    const bucket = retained.current;
    if (!bucket || bucket.key !== key) return;
    const publish = (status: SurfaceStatus, reason: string) => {
      if (disposed) return;
      bucket.status = bucket.capacity
        ? "capacity"
        : bucket.cleanupFailed && status === "receiving"
          ? "paused"
          : status;
      bucket.reason = bucket.capacity
        ? "Map capacity reached; prior scan retained"
        : bucket.cleanupFailed
          ? "Point cleanup interrupted; some old points may remain until a new session"
          : reason;
      const next: SurfaceState = {
        key,
        patches: bucket.patches,
        status: bucket.status,
        reason: bucket.reason,
        persistent: bucket.persistent,
        cellM: bucket.cellM,
      };
      setState((previous) =>
        previous.key === next.key &&
        previous.patches === next.patches &&
        previous.status === next.status &&
        previous.reason === next.reason &&
        previous.persistent === next.persistent &&
        previous.cellM === next.cellM
          ? previous
          : next,
      );
    };
    publish(
      bucket.persistent ? "paused" : "waiting",
      !map
        ? "Waiting for map identity"
        : "Feed disconnected or map identity unconfirmed; scan retained",
    );
    if (config.source !== "external" || !map || !connected) return;
    const feed = new SurfaceFeed(config.apiUrl.replace(/\/$/, ""));
    const poll = async () => {
      request = new AbortController();
      const signal = request.signal;
      const timeout = setTimeout(() => request?.abort(), 4000);
      let fusionTimeout: ReturnType<typeof setTimeout> | undefined;
      try {
        if (!bucket.client) throw Error("Map worker unavailable");
        if (bucket.client.busy) return;
        const capture = await feed.read(signal);
        if (!capture) {
          if (feed.issue)
            publish(
              bucket.persistent || bucket.patches.length ? "paused" : "waiting",
              feed.issue,
            );
          else if (bucket.persistent || bucket.patches.length)
            publish(
              "receiving",
              feed.ageMs > 3000
                ? `Rendering delayed RGB + depth (${(feed.ageMs / 1000).toFixed(1)} s)`
                : "Live RGB + depth",
            );
          return;
        }
        if (disposed || signal.aborted) return;
        const age =
          SURFACE_MAX_AGE_MS - (capture.expiresAt - performance.now());
        const reason =
          age > 3000
            ? `Rendering delayed RGB + depth (${(age / 1000).toFixed(1)} s)`
            : "Live RGB + depth";
        const expiresAt = Date.now() + capture.expiresAt - performance.now();
        clearTimeout(timeout);
        fusionTimeout = setTimeout(() => request?.abort(), 30000);
        let capturedAt = -Infinity;
        const result = await bucket.client.capture(
          capture.buffer,
          map,
          expiresAt,
          signal,
          (surface, points) => {
            if (
              disposed ||
              signal.aborted ||
              surfaceMapKey(surface) !== map ||
              surface.capturedAt <= capture.trackingLostCapture ||
              surface.capturedAt <= bucket.latest
            ) {
              closeImage(surface);
              return;
            }
            capturedAt = surface.capturedAt;
            const next = retainSurface(bucket.patches, surface, map);
            for (const old of bucket.patches)
              if (!next.includes(old)) closeImage(old);
            if (!next.includes(surface)) closeImage(surface);
            bucket.patches = next;
            ingestCaptured(points);
            publish("receiving", reason);
            return bucket.patches.map((patch) => patch.id);
          },
          capture.trackingLostCapture,
          bucket.latest,
          async (points) => {
            // A timed-out/disconnected caller still drains confirmed cleanup.
            // Source/map disposal changes this bucket and terminates its worker.
            if (retained.current !== bucket || bucket.key !== key) return;
            try {
              if (!(await ingestCaptured(points))) bucket.cleanupFailed = true;
            } catch {
              bucket.cleanupFailed = true;
            }
            if (bucket.cleanupFailed)
              publish("paused", "Point cleanup interrupted");
          },
        );
        if (disposed || signal.aborted) return;
        // Worker validates age before integration. A large retained mesh remains
        // historical geometry even if its fusion finishes after display freshness.
        if (result.retiredSurfaces?.length) {
          const updates = new Map(
            result.retiredSurfaces.map((p) => [p.id, p.indices]),
          );
          bucket.patches = bucket.patches.flatMap((patch) => {
            const indices = updates.get(patch.id);
            if (!indices) return [patch];
            if (!indices.length) {
              closeImage(patch);
              return [];
            }
            return [{ ...patch, indices }];
          });
        }
        bucket.persistent = result.patch;
        bucket.cellM = result.cellM;
        bucket.capacity = result.capacity ?? false;
        if (result.points && capturedAt > bucket.latest) {
          let applied = false;
          try {
            applied = await ingestCaptured(
              result.points,
              result.coverageEpoch !== bucket.coverageEpoch,
            );
          } catch {
            if (result.points.retirement) bucket.cleanupFailed = true;
          }
          if (!applied && result.points.retirement) bucket.cleanupFailed = true;
          if (disposed || signal.aborted) return;
          if (!applied)
            throw Error("Point cleanup unavailable; prior scan retained");
          bucket.coverageEpoch = result.coverageEpoch ?? bucket.coverageEpoch;
          bucket.latest = capturedAt;
        }
        feed.commit();
        publish("receiving", reason);
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
        if (!disposed) timer = setTimeout(() => void poll(), 50);
      }
    };
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      request?.abort();
    };
  }, [key, config.source, config.apiUrl, map, connected, ingestCaptured]);
  return state.key === key ? state : blank();
}
