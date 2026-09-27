/** Click-to-approach picking: what did a Navigate click land on first? */
export type Vec3 = readonly [number, number, number];

/** Nearest click distance along the ray that can pick a point (inside the rover's own body). */
const NEAR_M = 0.05;
/** Farthest measured point a click may select; beyond it depth is too sparse to trust. */
const FAR_M = 30;
/** A point counts as clicked within this perpendicular distance, growing with range. */
const MIN_TOLERANCE_M = 0.04;
const TOLERANCE_PER_M = 0.012;

/**
 * World (x, z) a Navigate click should approach: the nearest measured point
 * lying on the click ray, else where the ray meets the floor, else null.
 *
 * Points are scanned directly instead of through three.js raycasting, whose
 * cached bounding sphere goes stale while the cloud streams in. A point behind
 * the floor hit is ignored, so a click on bare floor still lands on the floor.
 */
export function pickApproachTarget(
  origin: Vec3,
  direction: Vec3,
  positions: ArrayLike<number>,
  count: number,
  floorY: number | null,
): [number, number] | null {
  const [ox, oy, oz] = origin;
  const length = Math.hypot(direction[0], direction[1], direction[2]);
  if (!(length > 0)) return null;
  const dx = direction[0] / length,
    dy = direction[1] / length,
    dz = direction[2] / length;
  const floorT =
    floorY !== null && dy < -1e-6
      ? (floorY - oy) / dy
      : Number.POSITIVE_INFINITY;
  const limit = Math.min(FAR_M, floorT + MIN_TOLERANCE_M);
  let best = -1,
    bestT = limit;
  const n = Math.min(count, Math.floor(positions.length / 3));
  for (let i = 0; i < n; i++) {
    const vx = positions[i * 3] - ox,
      vy = positions[i * 3 + 1] - oy,
      vz = positions[i * 3 + 2] - oz;
    const t = vx * dx + vy * dy + vz * dz;
    if (t <= NEAR_M || t >= bestT) continue;
    const tolerance = Math.max(MIN_TOLERANCE_M, TOLERANCE_PER_M * t);
    const perpendicular2 = vx * vx + vy * vy + vz * vz - t * t;
    if (perpendicular2 <= tolerance * tolerance) {
      best = i;
      bestT = t;
    }
  }
  if (best >= 0) return [positions[best * 3], positions[best * 3 + 2]];
  if (Number.isFinite(floorT) && floorT > 0 && floorT <= FAR_M)
    return [ox + dx * floorT, oz + dz * floorT];
  return null;
}
