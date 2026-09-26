import { PersistentSurfaceMap } from "./persistentSurfaceMap";
import type { SurfacePatch } from "./surfaceTypes";
const map = new PersistentSurfaceMap();
self.onmessage = (event: MessageEvent<{ id: number; patch: SurfacePatch }>) => {
  try {
    map.add(event.data.patch);
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
