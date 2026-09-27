import type { DetectionBox, Vec3 } from "./protocol";
import type { ReceivedDetections } from "./state";

/** Detector output older than this (by viewer receipt) is shown as stale, never as live. */
export const DETECTION_STALE_MS = 3000;

export const detectionAgeMs = (d: ReceivedDetections, now: number) =>
  Math.max(0, now - d.receivedAt);

export const detectionsLive = (d: ReceivedDetections | null, now: number) =>
  !!d && detectionAgeMs(d, now) <= DETECTION_STALE_MS;

export const className = (name: string) =>
  name === "potted plant"
    ? "Plant"
    : name.charAt(0).toUpperCase() + name.slice(1);

export interface LiveMarker {
  key: string;
  class: string;
  confidence: number;
  position: Vec3;
  object_id: string | null;
}

/**
 * 3D markers for the newest detections that the same capture's depth placed.
 * Stale output and boxes without a measured position place nothing.
 */
export function liveMarkers(
  d: ReceivedDetections | null,
  now: number,
): LiveMarker[] {
  if (!d || !detectionsLive(d, now)) return [];
  return d.frame.detections.flatMap((box, i) =>
    box.position
      ? [
          {
            key: `${d.frame.frame_id}:${i}`,
            class: box.class,
            confidence: box.confidence,
            position: box.position,
            object_id: box.object_id,
          },
        ]
      : [],
  );
}

/** A JPEG-pixel box as percentages of its own image, for an aspect-locked overlay. */
export function boxPercent(
  box: DetectionBox["box"],
  image: { width: number; height: number },
) {
  const [x1, y1, x2, y2] = box;
  return {
    left: (x1 / image.width) * 100,
    top: (y1 / image.height) * 100,
    width: ((x2 - x1) / image.width) * 100,
    height: ((y2 - y1) / image.height) * 100,
  };
}

export const detectionFrameKey = (d: ReceivedDetections) =>
  JSON.stringify([d.frame.session_id, d.frame.map_epoch, d.frame.frame_id]);

export function detectionImageUrl(apiUrl: string, d: ReceivedDetections) {
  const query = new URLSearchParams({
    session_id: String(d.frame.session_id),
    map_epoch: String(d.frame.map_epoch),
    frame_id: String(d.frame.frame_id),
  });
  return `${apiUrl.replace(/\/$/, "")}/capture/detections.jpg?${query}`;
}
