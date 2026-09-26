import { useEffect, useState } from "react";
import type { CapturedSurface } from "./surfaceTypes";
import {
  SurfaceTileBuffer,
  type RetainedSurfaceTile,
} from "./surfaceTileBuffer";
import type { SurfaceUpdate } from "./surfaceView.worker";
import type { CloudBounds } from "./pointCloud";

type View = {
  key: string;
  tiles: RetainedSurfaceTile[];
  recent: CapturedSurface[];
  triangles: number;
  capacity: boolean;
  bounds: CloudBounds | null;
};
const blank = (key: string): View => ({
  key,
  tiles: [],
  recent: [],
  triangles: 0,
  capacity: false,
  bounds: null,
});

export function useSurfaceView(
  source: string | null,
  map: string | null,
  active: boolean,
) {
  const key = JSON.stringify([source, map]);
  const [state, setState] = useState<View>(() => blank(key));
  const [worker, setWorker] = useState<Worker | null>(null);
  useEffect(() => {
    setState(blank(key));
    if (!source || !map) return;
    const base = new URL(source);
    base.protocol = base.protocol === "wss:" ? "https:" : "http:";
    const worker = new Worker(
      new URL("./surfaceView.worker.ts", import.meta.url),
      { type: "module" },
    );
    const tiles = new Map<string, RetainedSurfaceTile>();
    let recent: CapturedSurface[] = [];
    let disposed = false;
    worker.onmessage = ({ data }: MessageEvent<SurfaceUpdate>) => {
      if (disposed) {
        data.recent?.image?.close();
        return;
      }
      for (const tile of data.tiles) {
        const buffer =
          tiles.get(tile.id)?.buffer ?? new SurfaceTileBuffer(tile.id);
        buffer.apply(tile);
        tiles.set(tile.id, { buffer, revision: buffer.revision });
      }
      if (data.recent) {
        const incoming = data.recent;
        // Replace a redundant camera view, retain fine textures from other views.
        const next = recent.filter(
          (p) =>
            !(
              Math.hypot(
                ...p.cameraPosition.map(
                  (v, i) => v - incoming.cameraPosition[i],
                ),
              ) < 0.15 &&
              p.cameraForward.reduce(
                (sum, v, i) => sum + v * incoming.cameraForward[i],
                0,
              ) > 0.985
            ),
        );
        next.push(incoming);
        const bytes = (p: CapturedSurface) =>
          (p.image ? p.image.width * p.image.height * 4 : 0) +
          p.positions.byteLength +
          p.indices.byteLength +
          (p.uvs?.byteLength ?? 0);
        let size = next.reduce((sum, p) => sum + bytes(p), 0);
        while (next.length > 24 || size > 48 * 1024 * 1024)
          size -= bytes(next.shift()!);
        for (const old of recent) if (!next.includes(old)) old.image?.close();
        recent = next;
      }
      setState({
        key,
        tiles: [...tiles.values()],
        recent,
        triangles: data.triangles,
        capacity: data.capacity,
        bounds: data.bounds,
      });
    };
    worker.postMessage({ base: base.origin, map, active: false });
    setWorker(worker);
    return () => {
      disposed = true;
      worker.terminate();
      for (const patch of recent) patch.image?.close();
      setWorker(null);
    };
  }, [source, map, key]);
  useEffect(() => {
    worker?.postMessage({ active });
  }, [worker, active]);
  return state.key === key ? state : blank(key);
}
