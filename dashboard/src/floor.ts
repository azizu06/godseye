import type { Occupancy } from "./protocol";

/** Backend occupancy only accepts world Y in [-4, 4); anything else is not this map's floor. */
const FLOOR_LIMIT_M = 4;

/**
 * Confirmed world-Y floor from the backend occupancy estimate, or null when it
 * is unknown or invalid. Display only: measured scan geometry keeps its world
 * coordinates, and this never levels, rotates or calibrates anything.
 */
export function displayFloorY(occupancy: Occupancy | null): number | null {
  const y: unknown = occupancy?.floor_y;
  return typeof y === "number" &&
    Number.isFinite(y) &&
    y >= -FLOOR_LIMIT_M &&
    y < FLOOR_LIMIT_M
    ? y
    : null;
}

/** Existing small visual offsets above (or below) the floor, in meters. */
export const OVERLAY_OFFSET = {
  grid: -0.025,
  pick: -0.01,
  occupancy: 0.025,
  scope: 0.045,
  trajectory: 0.055,
  path: 0.075,
  scopeLabel: 0.085,
  approach: 0.09,
  rover: 0.1,
} as const;

/**
 * World Y for one floor overlay. An unknown floor keeps the previous AR-origin
 * placement (floor assumed at Y = 0); the scene labels that assumption.
 */
export const overlayY = (
  floor: number | null,
  overlay: keyof typeof OVERLAY_OFFSET,
) => (floor ?? 0) + OVERLAY_OFFSET[overlay];
