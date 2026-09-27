import { DepthContradiction, type DepthObservation } from "./depthRetirement";
import { MAX_SURFACE_PATCHES } from "./surfaceStore";
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
import {
  MAX_RETAINED_PEOPLE,
  personProofResults,
  type PersonClearance,
  type PersonProbe,
} from "./personMemory";
const map = new PersistentSurfaceMap();
const keyframes = new SurfaceKeyframes();
let coverageEpoch = 0;
let capacity = false;
let previousDepth: DepthObservation | undefined;
let completedCapture = -Infinity;
let recentGeometry: CapturedSurface[] = [];
let recentCursor = 0;
let recentFaces = new Map<string, number>();

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
    retainedSurfaceIds?: string[];
    /** Remembered people to test against this capture's two-view proof. */
    people?: PersonProbe[];
  }>,
) => {
  const { id } = event.data;
  let bitmap: ImageBitmap | undefined;
  const priorDepth = previousDepth;
  const priorCursor = recentCursor;
  const priorFaces = new Map(recentFaces);
  let rollbackRetirement: (() => void) | undefined;
  try {
    if (event.data.buffer) {
      const surface = decodeCaptureSurface(event.data.buffer, true);
      const valid = () =>
        JSON.stringify([surface.sessionId, surface.mapEpoch]) ===
          event.data.expectedMap && Date.now() <= (event.data.expiresAt ?? 0);
      if (surface.capturedAt <= (event.data.trackingLostCapture ?? -1))
        throw Error("Capture predates tracking loss");
      if (
        surface.capturedAt <= (event.data.latest ?? -Infinity) ||
        surface.capturedAt <= completedCapture
      ) {
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
      const { depthObservation: _depth, ...displaySurface } = surface;
      const recent: CapturedSurface = {
        ...displaySurface,
        ...compact,
        jpeg: undefined,
        image: canvas.transferToImageBitmap(),
      };
      // Only the UI knows whether a preview was accepted. Keep its acknowledged
      // geometry plus this one pending preview; an aborted preview cannot evict it.
      const retainedIds = new Set(
        (
          event.data.retainedSurfaceIds ??
          recentGeometry.map((patch) => patch.id)
        ).slice(-MAX_SURFACE_PATCHES),
      );
      recentGeometry = recentGeometry.filter((patch) =>
        retainedIds.has(patch.id),
      );
      if (compact.indices.length)
        recentGeometry.push({
          ...displaySurface,
          ...compactSurface(surface),
          jpeg: undefined,
        });
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
      const currentDepth = surface.depthObservation!;
      let retirement: DepthObservation[] | undefined;
      if (
        previousDepth &&
        previousDepth.sessionId === currentDepth.sessionId &&
        previousDepth.mapEpoch === currentDepth.mapEpoch &&
        previousDepth.capturedAt > (event.data.trackingLostCapture ?? -1) &&
        previousDepth.capturedAt < currentDepth.capturedAt
      )
        retirement = [previousDepth, currentDepth];
      if (
        !previousDepth ||
        currentDepth.capturedAt > previousDepth.capturedAt ||
        previousDepth.sessionId !== currentDepth.sessionId ||
        previousDepth.mapEpoch !== currentDepth.mapEpoch
      )
        previousDepth = currentDepth;
      const retiredSurfaces: { id: string; indices: Uint32Array }[] = [];
      let nextRecent = recentGeometry;
      let removed = 0;
      let clearedPeople: PersonClearance[] = [];
      if (retirement) {
        if (!valid()) throw Error("RGB-D expired before retirement");
        const evidence = new DepthContradiction(retirement);
        // The same calibrated two-view proof that retires geometry shows a
        // remembered person's old location is now empty; nothing else removes it.
        clearedPeople = personProofResults(
          evidence,
          (event.data.people ?? []).slice(0, MAX_RETAINED_PEOPLE),
        );
        const undo = map.retirementCheckpoint();
        removed = map.retire(evidence);
        if (removed) rollbackRetirement = undo;
        // Texture proof is also bounded/off-thread; each old patch resumes at its saved face.
        nextRecent = [...recentGeometry];
        const deadline = performance.now() + 24;
        for (let visited = 0; visited < recentGeometry.length; visited++) {
          const index = recentCursor % recentGeometry.length;
          recentCursor = (index + 1) % recentGeometry.length;
          const patch = recentGeometry[index];
          if (patch.capturedAt >= evidence.before) continue;
          const faces = patch.indices.length / 3,
            removedFaces = new Set<number>();
          let cursor = recentFaces.get(patch.id) ?? 0;
          for (let seen = 0; seen < faces; seen++) {
            if (seen % 64 === 0 && performance.now() >= deadline) break;
            const face = cursor % faces;
            cursor = (face + 1) % faces;
            const a = patch.indices[face * 3],
              b = patch.indices[face * 3 + 1],
              c = patch.indices[face * 3 + 2];
            if (
              evidence.triangle(
                patch.positions.subarray(a * 3, a * 3 + 3),
                patch.positions.subarray(b * 3, b * 3 + 3),
                patch.positions.subarray(c * 3, c * 3 + 3),
              )
            )
              removedFaces.add(face);
          }
          if (removedFaces.size) {
            const next = patch.indices.filter(
              (_, i) => !removedFaces.has(Math.floor(i / 3)),
            );
            nextRecent[index] = { ...patch, indices: next };
            retiredSurfaces.push({ id: patch.id, indices: next.slice() });
            cursor %= Math.max(1, next.length / 3);
          }
          recentFaces.set(patch.id, cursor);
          if (performance.now() >= deadline) break;
        }
        nextRecent = nextRecent.filter((patch) => patch.indices.length);
        const retainedIds = new Set(nextRecent.map((patch) => patch.id));
        recentFaces = new Map(
          [...recentFaces].filter(([id]) => retainedIds.has(id)),
        );
        points.retirement = retirement;
      }
      // Capacity is re-evaluated per view: bounded coarsening lets later views
      // in, and a saturated map rejects them cheaply without new work.
      if (
        observation.indices.length &&
        (removed > 0 || keyframes.shouldIntegrate(observation))
      ) {
        try {
          const colored = bakeSurfaceColors(observation, pixels);
          if (!valid()) throw Error("RGB-D expired before integration");
          const accepted = map.add(colored);
          if (accepted.reset) coverageEpoch++;
          if (accepted.retained)
            points.covered = retainedCoverage(surface, accepted.retained);
          if (removed) keyframes.clear();
          keyframes.remember(observation);
          capacity = map.atCapacity;
        } catch (error) {
          if (error instanceof Error && /capacity/i.test(error.message)) {
            capacity = true;
            if (removed) keyframes.clear();
          } else throw error;
        }
      }
      recentGeometry = nextRecent;
      map.commitRetirement();
      completedCapture = surface.capturedAt;
      const delta = map.takeDelta();
      self.postMessage(
        {
          id,
          delta,
          cellM: map.cellM,
          points,
          coverageEpoch,
          capacity,
          retiredSurfaces,
          clearedPeople,
        },
        {
          transfer: [
            delta.positions.buffer,
            delta.colors.buffer,
            delta.indices.buffer,
            points.positions.buffer,
            points.colors.buffer,
            ...(points.covered ? [points.covered.buffer] : []),
            ...retiredSurfaces.map((patch) => patch.indices.buffer),
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
    rollbackRetirement?.();
    previousDepth = priorDepth;
    recentCursor = priorCursor;
    recentFaces = priorFaces;
    self.postMessage({
      id,
      error: error instanceof Error ? error.message : "Map integration failed",
    });
  } finally {
    bitmap?.close();
  }
};
