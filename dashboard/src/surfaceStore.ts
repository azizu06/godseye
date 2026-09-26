import type { CapturedSurface } from "./surfaceTypes";
export const MAX_SURFACE_PATCHES = 24;
export const MAX_SURFACE_BYTES = 48 * 1024 * 1024;
export const surfaceMapKey = (p: CapturedSurface) =>
  JSON.stringify([p.sessionId, p.mapEpoch]);
const bytes = (p: CapturedSurface) =>
  p.positions.byteLength +
  p.indices.byteLength +
  (p.uvs?.byteLength ?? 0) +
  (p.jpeg?.byteLength ?? 0) +
  (p.image ? p.image.width * p.image.height * 4 : 0);
/** Replace equivalent viewpoints rather than piling coincident surfaces forever. */
export function retainSurface(
  current: CapturedSurface[],
  incoming: CapturedSurface,
  expectedMap: string,
): CapturedSurface[] {
  if (
    surfaceMapKey(incoming) !== expectedMap ||
    !incoming.indices.length ||
    bytes(incoming) > MAX_SURFACE_BYTES
  )
    return current;
  if (current.some((p) => p.capturedAt >= incoming.capturedAt)) return current;
  const next = current.filter(
    (p) =>
      surfaceMapKey(p) === expectedMap &&
      !(
        Math.hypot(
          ...p.cameraPosition.map((v, i) => v - incoming.cameraPosition[i]),
        ) < 0.25 &&
        p.cameraForward.reduce(
          (sum, v, i) => sum + v * incoming.cameraForward[i],
          0,
        ) > 0.97
      ),
  );
  next.push(incoming);
  let size = next.reduce((n, p) => n + bytes(p), 0);
  while (next.length > MAX_SURFACE_PATCHES || size > MAX_SURFACE_BYTES)
    size -= bytes(next.shift()!);
  return next;
}
