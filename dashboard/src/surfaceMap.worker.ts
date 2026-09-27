import { SurfaceKeyframes, type SurfaceViewpoint } from "./surfaceKeyframes";
import {
  bakeSurfaceColors,
  compactSurface,
  type ColorPixels,
} from "./surfaceColor";
import { PersistentSurfaceMap } from "./persistentSurfaceMap";
import { decodeCaptureSurface } from "./captureSurface";
import { retainedCoverage } from "./retainedCoverage";
import type { CapturedSurface, SurfacePatch } from "./surfaceTypes";
import type { CapturedPoints } from "./pointCloud";
const map = new PersistentSurfaceMap();
const keyframes = new SurfaceKeyframes();
let coverageEpoch = 0;
let capacity = false;
self.onmessage = async (
  event: MessageEvent<{
    id: number;
    patch: SurfacePatch;
    image?: ColorPixels;
    viewpoint?: SurfaceViewpoint;
    buffer?: ArrayBuffer;
    expectedMap?: string;
    expiresAt?: number;
    trackingLostCapture?: number;
    latest?: number;
  }>,
) => {
  const { id } = event.data;
  let bitmap: ImageBitmap | undefined;
  try {
    if (event.data.buffer) {
      const surface = decodeCaptureSurface(event.data.buffer, true);
      const valid = () =>
        JSON.stringify([surface.sessionId, surface.mapEpoch]) ===
          event.data.expectedMap && Date.now() <= (event.data.expiresAt ?? 0);
      if (surface.capturedAt <= (event.data.trackingLostCapture ?? -1))
        throw Error("Capture predates tracking loss");
      if (surface.capturedAt <= (event.data.latest ?? -Infinity)) {
        const delta = map.takeDelta();
        self.postMessage(
          { id, delta, cellM: map.cellM, coverageEpoch, capacity },
          {
            transfer: [
              delta.positions.buffer,
              delta.colors.buffer,
              delta.indices.buffer,
            ],
          },
        );
        return;
      }
      if (!valid())
        throw Error(
          "Capture map identity changed or RGB-D expired during transfer",
        );
      if (!surface.positions.length || !surface.jpeg)
        throw Error("Latest frame has no usable depth");
      bitmap = await createImageBitmap(
        new Blob([surface.jpeg as BlobPart], { type: "image/jpeg" }),
      );
      const scale = Math.min(1, 1280 / Math.max(bitmap.width, bitmap.height));
      const canvas = new OffscreenCanvas(
        Math.max(1, Math.round(bitmap.width * scale)),
        Math.max(1, Math.round(bitmap.height * scale)),
      );
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) throw Error("Color decoding unavailable");
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
      const compact = compactSurface(surface);
      const colors = bakeSurfaceColors(
        { ...surface, indices: new Uint32Array() },
        pixels,
      ).colors!;
      const points: CapturedPoints = {
        sessionId: surface.sessionId,
        mapEpoch: surface.mapEpoch,
        frameId: surface.frameId,
        capturedAt: surface.capturedAt,
        positions: surface.positions,
        colors,
      };
      if (!valid()) throw Error("RGB-D expired during decoding");
      // An ImageBitmap ignores WebGL flipY; transfer an explicitly flipped texture.
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.translate(0, canvas.height);
      context.scale(1, -1);
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const recent: CapturedSurface = {
        ...surface,
        ...compact,
        jpeg: undefined,
        image: canvas.transferToImageBitmap(),
      };
      const previewPoints = {
        ...points,
        positions: points.positions.slice(),
        colors: points.colors.slice(),
      };
      self.postMessage(
        { id, preview: { surface: recent, points: previewPoints } },
        {
          transfer: [
            recent.image as ImageBitmap,
            recent.positions.buffer,
            recent.indices.buffer,
            recent.uvs!.buffer,
            previewPoints.positions.buffer,
            previewPoints.colors.buffer,
          ],
        },
      );
      // Compact buffers were transferred for preview; reconstruct their bounded
      // views only when this observation contributes new retained geometry.
      const observation = {
        ...compactSurface(surface),
        ...event.data.viewpoint,
        sessionId: surface.sessionId,
        mapEpoch: surface.mapEpoch,
        cameraPosition: surface.cameraPosition,
        cameraForward: surface.cameraForward,
      };
      // Capacity is re-evaluated per view: bounded coarsening lets later views
      // in, and a saturated map rejects them cheaply without new work.
      if (
        observation.indices.length &&
        keyframes.shouldIntegrate(observation)
      ) {
        try {
          const colored = bakeSurfaceColors(observation, pixels);
          if (!valid()) throw Error("RGB-D expired before integration");
          const accepted = map.add(colored);
          if (accepted.reset) coverageEpoch++;
          if (accepted.retained)
            points.covered = retainedCoverage(surface, accepted.retained);
          keyframes.remember(observation);
          capacity = map.atCapacity;
        } catch (error) {
          if (error instanceof Error && /capacity/i.test(error.message))
            capacity = true;
          else throw error;
        }
      }
      const delta = map.takeDelta();
      self.postMessage(
        { id, delta, cellM: map.cellM, points, coverageEpoch, capacity },
        {
          transfer: [
            delta.positions.buffer,
            delta.colors.buffer,
            delta.indices.buffer,
            points.positions.buffer,
            points.colors.buffer,
            ...(points.covered ? [points.covered.buffer] : []),
          ],
        },
      );
    } else {
      // Existing pure-geometry path remains available to fixtures and importers.
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
      self.postMessage({ id, patch: map.snapshot(), cellM: map.cellM });
    }
  } catch (error) {
    self.postMessage({
      id,
      error: error instanceof Error ? error.message : "Map integration failed",
    });
  } finally {
    bitmap?.close();
  }
};
