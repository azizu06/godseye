import { decodeCaptureSurface } from "./captureSurface";
import { bakeSurfaceColors } from "./surfaceColor";
import { SurfaceTiles } from "./surfaceTiles";
import type { CapturedSurface, SurfacePatch } from "./surfaceTypes";
import type { CloudBounds } from "./pointCloud";

export type SurfaceUpdate = {
  tiles: SurfacePatch[];
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
const record = (v: unknown): Record<string, any> =>
  v && typeof v === "object" && !Array.isArray(v) ? v : {};
const map = new SurfaceTiles();
let base = "",
  identity = "",
  active = false,
  busy = false,
  latest = -1;
let request: AbortController | undefined;
let token = "";

async function boundedBody(response: Response, signal: AbortSignal) {
  const maximum = 32 * 1024 * 1024;
  if (!response.ok || Number(response.headers.get("Content-Length")) > maximum)
    throw Error("Capture unavailable");
  const reader = response.body?.getReader();
  if (!reader) throw Error("Capture empty");
  const chunks: Uint8Array[] = [];
  let count = 0;
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) break;
      count += value.length;
      if (count > maximum) throw Error("Capture too large");
      chunks.push(value);
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
  const bytes = new Uint8Array(count);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return bytes.buffer;
}

async function poll() {
  if (!active || busy) return;
  busy = true;
  request = new AbortController();
  const { signal } = request;
  const timeout = setTimeout(() => request?.abort(), 3000);
  let bitmap: ImageBitmap | undefined;
  try {
    const response = await fetch(`${base}/capture/status`, {
      signal,
      cache: "no-store",
    });
    if (!response.ok) return;
    const status = record(await response.json());
    if (status.tracking !== "normal" || record(status.health).phone !== "ok")
      return;
    const rich = record(record(record(status.rich).packets).frame),
      legacy = record(status.frame);
    const richTime = Number(rich.t_capture),
      legacyTime = Number(record(legacy.metadata).t_capture);
    const sections = Array.isArray(rich.sections)
      ? rich.sections.map((s: unknown) => record(s).name)
      : [];
    const useRich =
      ["rgb", "raw_depth", "raw_confidence"].every((n) =>
        sections.includes(n),
      ) &&
      typeof rich.age_ms === "number" &&
      rich.age_ms <= 1000 &&
      (!Number.isFinite(legacyTime) || richTime >= legacyTime - 0.2);
    const frame = useRich ? rich : legacy;
    const nextToken = String(
      useRich ? (frame.token ?? "") : (frame.capture_id ?? ""),
    );
    if (
      !nextToken ||
      token === nextToken ||
      typeof frame.age_ms !== "number" ||
      frame.age_ms > 1000
    )
      return;
    const packet = await fetch(
      `${base}/capture/${useRich ? "rich/frame" : "frame"}.bin`,
      { signal, cache: "no-store" },
    );
    const surface = decodeCaptureSurface(await boundedBody(packet, signal));
    if (
      !active ||
      signal.aborted ||
      JSON.stringify([surface.sessionId, surface.mapEpoch]) !== identity ||
      surface.capturedAt <= latest ||
      surface.capturedAt <= (status.tracking_lost_capture ?? -1)
    )
      return;
    token = nextToken;
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
    if (!active || signal.aborted) return;
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
        tile.positions.buffer,
        tile.colors!.buffer,
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
  if (data.base) base = data.base;
  if (data.map) identity = data.map;
  active = data.active;
  if (!active) request?.abort();
  else void poll();
};
setInterval(() => void poll(), 200);
