import { MAX_SURFACE_PATCHES } from "./surfaceStore";
import { SurfaceBuffer, type SurfaceDelta } from "./surfaceBuffer";
import type { CapturedPoints } from "./pointCloud";
import type { CapturedSurface } from "./surfaceTypes";
import type { SurfaceViewpoint } from "./surfaceKeyframes";
import type { ColorPixels } from "./surfaceColor";
import type { SurfacePatch } from "./surfaceTypes";
export interface MapSnapshot {
  patch: SurfacePatch | null;
  cellM: number;
  points?: CapturedPoints;
  coverageEpoch?: number;
  capacity?: boolean;
  delta?: SurfaceDelta;
  retiredSurfaces?: { id: string; indices: Uint32Array }[];
}
/** At most one posted operation, even after its caller aborts while fusion continues. */
export class SurfaceMapClient {
  private worker: Worker;
  private sequence = 0;
  private buffer = new SurfaceBuffer();
  private outstanding = false;
  private displayedIds = new Set<string>();
  private pendingRetirement = new Map<string, Uint32Array>();
  private cancelPending: (() => void) | null = null;
  get busy() {
    return this.outstanding;
  }
  constructor() {
    this.worker = new Worker(
      new URL("./surfaceMap.worker.ts", import.meta.url),
      { type: "module" },
    );
  }
  capture(
    buffer: ArrayBuffer,
    expectedMap: string,
    expiresAt: number,
    signal: AbortSignal,
    preview: (
      surface: CapturedSurface,
      points: CapturedPoints,
    ) => readonly string[] | void,
    trackingLostCapture = -1,
    latest = -Infinity,
    completedAfterAbort?: (points: CapturedPoints) => void | Promise<unknown>,
  ): Promise<MapSnapshot> {
    return this.request(
      { buffer, expectedMap, expiresAt, trackingLostCapture, latest },
      signal,
      preview,
      [buffer],
      completedAfterAbort,
    );
  }
  add(
    patch: SurfacePatch,
    signal: AbortSignal,
    image?: ColorPixels,
    viewpoint?: SurfaceViewpoint,
  ): Promise<MapSnapshot> {
    return this.request({ patch, image, viewpoint }, signal);
  }
  private request(
    payload: object,
    signal: AbortSignal,
    preview?: (
      surface: CapturedSurface,
      points: CapturedPoints,
    ) => readonly string[] | void,
    transfer: Transferable[] = [],
    completedAfterAbort?: (points: CapturedPoints) => void | Promise<unknown>,
  ): Promise<MapSnapshot> {
    if (this.outstanding) return Promise.reject(Error("Map worker busy"));
    if (signal.aborted)
      return Promise.reject(Error("Map integration cancelled"));
    this.outstanding = true;
    return new Promise((resolve, reject) => {
      const id = ++this.sequence;
      let settled = false;
      const cleanup = () => {
        signal.removeEventListener("abort", abort);
        this.worker.removeEventListener("message", message);
        this.worker.removeEventListener("error", error);
        this.outstanding = false;
        this.cancelPending = null;
      };
      const fail = (reason: Error) => {
        cleanup();
        if (!settled) {
          settled = true;
          reject(reason);
        }
      };
      // Reject the caller promptly, but drain this response before admitting more work.
      const abort = () => {
        signal.removeEventListener("abort", abort);
        if (!settled) {
          settled = true;
          reject(Error("Map integration cancelled"));
        }
      };
      const error = () => fail(Error("Map worker unavailable"));
      const message = (
        event: MessageEvent<
          MapSnapshot & {
            id: number;
            error?: string;
            preview?: { surface: CapturedSurface; points: CapturedPoints };
          }
        >,
      ) => {
        if (event.data.id !== id) return;
        if (event.data.preview) {
          if (!settled) {
            const retainedIds = preview?.(
              event.data.preview.surface,
              event.data.preview.points,
            );
            if (retainedIds)
              this.displayedIds = new Set(
                retainedIds.slice(-MAX_SURFACE_PATCHES),
              );
          } else
            (
              event.data.preview.surface.image as ImageBitmap | undefined
            )?.close?.();
          return;
        }
        // Drain deltas even when a caller aborted, so the next append stays aligned.
        if (event.data.delta)
          event.data.patch = this.buffer.apply(event.data.delta);
        for (const update of event.data.retiredSurfaces ?? [])
          if (this.displayedIds.has(update.id))
            this.pendingRetirement.set(update.id, update.indices);
        for (const id of this.pendingRetirement.keys())
          if (!this.displayedIds.has(id)) this.pendingRetirement.delete(id);
        if (settled) {
          if (
            !event.data.error &&
            event.data.points?.retirement &&
            completedAfterAbort
          ) {
            // Preserve single-flight until confirmed point cleanup is acknowledged.
            // Disposal/reset can settle the callback without touching a new map.
            Promise.resolve()
              .then(() => completedAfterAbort(event.data.points!))
              .catch(() => {})
              .finally(cleanup);
          } else cleanup();
          return;
        }
        cleanup();
        settled = true;
        if (event.data.error) reject(Error(event.data.error));
        else {
          event.data.retiredSurfaces = [...this.pendingRetirement].map(
            ([id, indices]) => ({ id, indices }),
          );
          this.pendingRetirement.clear();
          resolve(event.data);
        }
      };
      this.cancelPending = () => fail(Error("Map integration cancelled"));
      signal.addEventListener("abort", abort, { once: true });
      this.worker.addEventListener("message", message);
      this.worker.addEventListener("error", error);
      try {
        this.worker.postMessage(
          { id, ...payload, retainedSurfaceIds: [...this.displayedIds] },
          transfer,
        );
      } catch {
        fail(Error("Map worker unavailable"));
      }
    });
  }
  dispose() {
    this.cancelPending?.();
    this.worker.terminate();
  }
}
