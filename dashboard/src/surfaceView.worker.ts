import { decodeCaptureSurface } from "./captureSurface";
import { bakeSurfaceColors } from "./surfaceColor";
import { SurfaceTiles } from "./surfaceTiles";
import { SurfaceFeed } from "./surfaceFeed";
import type { CapturedSurface, SurfaceTileUpdate } from "./surfaceTypes";
import type { CloudBounds } from "./pointCloud";

export type SurfaceUpdate = {
  tiles: SurfaceTileUpdate[];
  recent?: CapturedSurface;
  triangles: number;
  capacity: boolean;
  bounds: CloudBounds | null;
};
const worker = self as unknown as {
  onmessage: (
    event: MessageEvent<{ base?: string; map?: string; active: boolean }>,
  ) => void;
  postMessage: (message: SurfaceUpdate, transfer: Transferable[]) => void;
};
const map = new SurfaceTiles();
let feed: SurfaceFeed;
let identity = "",
  active = false,
  busy = false,
  latest = -1;
let request: AbortController | undefined;
async function poll() {
  if (!active || busy) return;
  busy = true;
  request = new AbortController();
  const { signal } = request;
  const timeout = setTimeout(() => request?.abort(), 3000);
  let bitmap: ImageBitmap | undefined;
  try {
    const capture = await feed.read(signal);
    if (!capture) return;
    const surface = decodeCaptureSurface(capture.buffer);
    if (
      !active ||
      signal.aborted ||
      JSON.stringify([surface.sessionId, surface.mapEpoch]) !== identity ||
      surface.capturedAt <= latest ||
      surface.capturedAt <= capture.trackingLostCapture ||
      performance.now() > capture.expiresAt
    )
      return;
    if (!surface.indices.length || !surface.jpeg) return;
    bitmap = await createImageBitmap(
      new Blob([surface.jpeg as BlobPart], { type: "image/jpeg" }),
    );
    const scale = Math.min(1, 1280 / Math.max(bitmap.width, bitmap.height));
    const canvas = new OffscreenCanvas(
      Math.max(1, Math.round(bitmap.width * scale)),
      Math.max(1, Math.round(bitmap.height * scale)),
    );
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) throw Error("Image decoding unavailable");
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const colored = bakeSurfaceColors(
      surface,
      context.getImageData(0, 0, canvas.width, canvas.height),
    );
    if (!active || signal.aborted || performance.now() > capture.expiresAt)
      return;
    const tiles = map.add(colored);
    // ImageBitmap ignores WebGL's flipY flag: flip pixels explicitly before transfer.
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.translate(0, canvas.height);
    context.scale(1, -1);
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const image = canvas.transferToImageBitmap();
    const recent = { ...surface, jpeg: undefined, image };
    latest = surface.capturedAt;
    const transfer: Transferable[] = [
      image,
      recent.positions.buffer,
      recent.indices.buffer,
      recent.uvs!.buffer,
    ];
    for (const tile of tiles)
      transfer.push(
        tile.spans.buffer,
        tile.positions.buffer,
        tile.colors.buffer,
        tile.indices.buffer,
      );
    worker.postMessage(
      {
        tiles,
        recent,
        triangles: map.triangles,
        capacity: map.capacity,
        bounds: map.bounds(),
      },
      transfer,
    );
  } catch {
    // Retained geometry stays visible. A missing capture endpoint keeps the point fallback.
  } finally {
    bitmap?.close();
    clearTimeout(timeout);
    busy = false;
  }
}
worker.onmessage = ({ data }) => {
  if (data.base) feed = new SurfaceFeed(data.base);
  if (data.map) identity = data.map;
  active = data.active;
  if (!active) request?.abort();
  else void poll();
};
setInterval(() => void poll(), 200);
