import type { Vec3 } from "./protocol";

// Apple's ARKit scene-depth sample documents a maximum supported value of 5 m.
// https://developer.apple.com/documentation/arkit/displaying-a-point-cloud-using-scene-depth
export const LIDAR_RANGE_M = 5;
// Illustrative angle only: /live v1 does not provide camera intrinsics.
const ILLUSTRATIVE_HALF_ANGLE = Math.PI / 6;
export function scopeArc(radius = LIDAR_RANGE_M): Vec3[] {
  return Array.from({ length: 33 }, (_, i) => {
    const angle =
      -ILLUSTRATIVE_HALF_ANGLE + (i / 32) * 2 * ILLUSTRATIVE_HALF_ANGLE;
    return [Math.sin(angle) * radius, 0, Math.cos(angle) * radius];
  });
}
export function scopeTriangles(): Float32Array {
  const arc = scopeArc();
  return new Float32Array(
    arc.slice(1).flatMap((point, i) => [0, 0, 0, ...arc[i], ...point]),
  );
}
