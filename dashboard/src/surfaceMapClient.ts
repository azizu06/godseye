import type { SurfaceViewpoint } from "./surfaceKeyframes";
import type { ColorPixels } from "./surfaceColor";
import type { SurfacePatch } from "./surfaceTypes";
export interface MapSnapshot {
  patch: SurfacePatch | null;
  cellM: number;
}
/** At most one posted operation, even after its caller aborts while fusion continues. */
export class SurfaceMapClient {
  private worker: Worker;
  private sequence = 0;
  private outstanding = false;
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
  add(
    patch: SurfacePatch,
    signal: AbortSignal,
    image?: ColorPixels,
    viewpoint?: SurfaceViewpoint,
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
        event: MessageEvent<MapSnapshot & { id: number; error?: string }>,
      ) => {
        if (event.data.id !== id) return;
        cleanup();
        if (settled) return;
        settled = true;
        if (event.data.error) reject(Error(event.data.error));
        else resolve(event.data);
      };
      this.cancelPending = () => fail(Error("Map integration cancelled"));
      signal.addEventListener("abort", abort, { once: true });
      this.worker.addEventListener("message", message);
      this.worker.addEventListener("error", error);
      try {
        this.worker.postMessage({ id, patch, image, viewpoint });
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
