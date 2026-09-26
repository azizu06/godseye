import { decodeCaptureSurface } from "./captureSurface";
import { bakeSurfaceColors } from "./surfaceColor";
import { SurfaceTiles } from "./surfaceTiles";
import { SurfaceEvidence } from "./surfaceEvidence";
import { surfaceCoverage, surfaceSupport } from "./surfaceCoverage";
import { SurfaceFeed, SURFACE_MAX_AGE_MS } from "./surfaceFeed";
import type { CapturedSurface, SurfaceTileUpdate } from "./surfaceTypes";
import type { CloudBounds, CapturedPoints } from "./pointCloud";

export type SurfaceUpdate = {
  tiles: SurfaceTileUpdate[];
  recent?: CapturedSurface;
  triangles: number;
  capacity: boolean;
  bounds: CloudBounds | null;
  frameAgeMs: number;
  points: CapturedPoints;
};
export type SurfaceMessage = SurfaceUpdate | { issue: string };
const worker = self as unknown as {
  onmessage: (
    event: MessageEvent<{ base?: string; map?: string; active: boolean }>,
  ) => void;
  postMessage: (message: SurfaceMessage, transfer?: Transferable[]) => void;
};
const map = new SurfaceTiles();
const evidence = new SurfaceEvidence();
let feed: SurfaceFeed;
let identity = "",
  active = false,
  busy = false,
  latest = -1;
let request: AbortController | undefined;
let lastIssue = "";
function reportIssue(issue: string) {
  if (issue !== lastIssue) worker.postMessage({ issue });
  lastIssue = issue;
}
async function poll() {
  if (!active || busy) return;
  busy = true;
  request = new AbortController();
  const { signal } = request;
  const timeout = setTimeout(() => request?.abort(), SURFACE_MAX_AGE_MS);
  let bitmap: ImageBitmap | undefined;
  try {
    const capture = await feed.read(signal);
    if (!capture) return;
    const surface = decodeCaptureSurface(capture.buffer, true);
    if (
      !active ||
      signal.aborted ||
      JSON.stringify([surface.sessionId, surface.mapEpoch]) !== identity ||
      surface.capturedAt <= latest ||
      surface.capturedAt <= capture.trackingLostCapture ||
      performance.now() > capture.expiresAt
    )
      return;
    if (!surface.positions.length || !surface.jpeg) {
      reportIssue("Latest frame has no usable high-confidence depth");
      return;
    }
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
    const confirmed = evidence.confirm(surface, surfaceSupport(surface));
    const retained = new Uint32Array(confirmed.length);
    let retainedCount = 0;
    const tiles = confirmed.length
      ? map.add({ ...colored, indices: confirmed }, (a, b, c) => {
          retained[retainedCount++] = a;
          retained[retainedCount++] = b;
          retained[retainedCount++] = c;
        })
      : [];
    // ImageBitmap ignores WebGL's flipY flag: flip pixels explicitly before transfer.
    let recent: CapturedSurface | undefined;
    if (confirmed.length) {
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.translate(0, canvas.height);
      context.scale(1, -1);
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      recent = {
        ...surface,
        indices: confirmed,
        jpeg: undefined,
        image: canvas.transferToImageBitmap(),
      };
    }
    const points: CapturedPoints = {
      sessionId: surface.sessionId,
      mapEpoch: surface.mapEpoch,
      frameId: surface.frameId,
      capturedAt: surface.capturedAt,
      positions: surface.positions.slice(),
      colors: colored.colors!,
      covered: surfaceCoverage(surface, retained.subarray(0, retainedCount)),
    };
    latest = surface.capturedAt;
    lastIssue = "";
    const transfer: Transferable[] = [
      points.positions.buffer,
      points.colors.buffer,
      points.covered!.buffer,
    ];
    if (recent)
      transfer.push(
        recent.image!,
        recent.positions.buffer,
        recent.indices.buffer,
        recent.uvs!.buffer,
      );
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
        points,
        triangles: map.triangles,
        capacity: map.capacity,
        bounds: map.bounds(),
        frameAgeMs: Math.max(
          0,
          SURFACE_MAX_AGE_MS - (capture.expiresAt - performance.now()),
        ),
      },
      transfer,
    );
  } catch (error) {
    // Preserve the scan, but expose why new frames cannot be rendered.
    if (active)
      reportIssue(
        signal.aborted
          ? "Capture download timed out"
          : error instanceof Error
            ? error.message.slice(0, 160)
            : "Surface processing failed",
      );
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
setInterval(() => void poll(), 50);
