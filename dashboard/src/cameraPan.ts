/** Camera-to-target distance (m) at which a drag pans at OrbitControls' stock speed; near the default view. */
export const PAN_REFERENCE_DISTANCE_M = 12;
const MIN_PAN_SPEED = 0.3;
const MAX_PAN_SPEED = 6;

/**
 * OrbitControls pan speed that makes one drag move about the same world
 * distance at any zoom. Stock panning scales with camera-to-target distance,
 * which crawls when zoomed in and races when zoomed out.
 */
export function panSpeedForDistance(distance: number): number {
  if (!Number.isFinite(distance) || distance <= 0) return 1;
  return Math.min(
    MAX_PAN_SPEED,
    Math.max(MIN_PAN_SPEED, PAN_REFERENCE_DISTANCE_M / distance),
  );
}
