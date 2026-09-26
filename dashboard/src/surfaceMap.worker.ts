import { SurfaceKeyframes, type SurfaceViewpoint } from "./surfaceKeyframes";
import { bakeSurfaceColors, type ColorPixels } from "./surfaceColor";
import { PersistentSurfaceMap } from "./persistentSurfaceMap";
import type { SurfacePatch } from "./surfaceTypes";
const map = new PersistentSurfaceMap();
const keyframes = new SurfaceKeyframes();
self.onmessage = (
  event: MessageEvent<{
    id: number;
    patch: SurfacePatch;
    image?: ColorPixels;
    viewpoint?: SurfaceViewpoint;
  }>,
) => {
  try {
    const observation = event.data.viewpoint
      ? { ...event.data.patch, ...event.data.viewpoint }
      : null;
    if (!observation || keyframes.shouldIntegrate(observation)) {
      map.add(
        event.data.image
          ? bakeSurfaceColors(event.data.patch, event.data.image)
          : event.data.patch,
      );
      if (observation) keyframes.remember(observation);
    }
    // A skipped redundant view still returns the latest worker snapshot, which
    // may include a previous job whose caller was cancelled after it committed.
    self.postMessage({
      id: event.data.id,
      patch: map.snapshot(),
      cellM: map.cellM,
    });
  } catch (error) {
    self.postMessage({
      id: event.data.id,
      error: error instanceof Error ? error.message : "Map integration failed",
    });
  }
};
