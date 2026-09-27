import type { CapturedSurface, SurfacePatch } from "./surfaceTypes";
export type SurfaceViewpoint = Pick<
  CapturedSurface,
  "sessionId" | "mapEpoch" | "cameraPosition" | "cameraForward"
>;
type Observation = SurfacePatch & SurfaceViewpoint;

const CELL_M = 0.05;
const MAX_REFERENCES = 16;
const MAX_PROBES = 12_000;
type Point = [number, number, number];
type Reference = {
  map: string;
  position: Point;
  forward: Point;
  cells: Map<string, Point[]>;
};
const coordinates = (p: Point) => p.map((v) => Math.floor(v / CELL_M));
const key = (p: number[]) => p.join(",");
const mapKey = (p: Observation) => JSON.stringify([p.sessionId, p.mapEpoch]);
function probes(patch: Observation): Point[] | null {
  const points: Point[] = [],
    used = new Set<number>();
  const at = (i: number): Point => [
    patch.positions[i * 3],
    patch.positions[i * 3 + 1],
    patch.positions[i * 3 + 2],
  ];
  for (let i = 0; i < patch.indices.length; i += 3) {
    const ids = [...patch.indices.subarray(i, i + 3)];
    for (const id of ids)
      if (!used.has(id)) {
        used.add(id);
        points.push(at(id));
      }
    points.push(
      [0, 1, 2].map(
        (axis) =>
          ids.reduce((sum, id) => sum + patch.positions[id * 3 + axis], 0) / 3,
      ) as Point,
    );
    if (points.length > MAX_PROBES) return null;
  }
  return points;
}
/** Approximate geometry novelty, not registration or proof of identical surfaces.
 * Every referenced vertex and face centroid must lie within 5 cm of accepted
 * support before a near-identical view can skip persistent fusion. Images still
 * refresh, and any unsupported sample admits the entire incoming observation.
 */
export class SurfaceKeyframes {
  private references: Reference[] = [];
  clear() {
    this.references = [];
  }
  get size() {
    return this.references.length;
  }
  shouldIntegrate(patch: Observation): boolean {
    const near = this.references.filter(
      (reference) =>
        reference.map === mapKey(patch) &&
        Math.hypot(
          ...patch.cameraPosition.map((v, i) => v - reference.position[i]),
        ) < 0.1 &&
        patch.cameraForward.reduce(
          (sum, v, i) => sum + v * reference.forward[i],
          0,
        ) > Math.cos((5 * Math.PI) / 180),
    );
    if (!near.length) return true;
    const points = probes(patch);
    if (!points || !points.length) return true;
    return !near.some((reference) =>
      points.every((point) => {
        const [x, y, z] = coordinates(point);
        // Check distance as well as neighbor buckets: grid adjacency alone has
        // a much larger tolerance, especially across a diagonal cell boundary.
        for (let dx = -1; dx <= 1; dx++)
          for (let dy = -1; dy <= 1; dy++)
            for (let dz = -1; dz <= 1; dz++)
              for (const old of reference.cells.get(
                key([x + dx, y + dy, z + dz]),
              ) ?? [])
                if (
                  (point[0] - old[0]) ** 2 +
                    (point[1] - old[1]) ** 2 +
                    (point[2] - old[2]) ** 2 <=
                  CELL_M ** 2
                )
                  return true;
        return false;
      }),
    );
  }
  remember(patch: Observation): void {
    const points = probes(patch);
    if (!points || !points.length) return;
    const cells = new Map<string, Point[]>();
    for (const point of points) {
      const id = key(coordinates(point)),
        bucket = cells.get(id);
      if (bucket) bucket.push(point);
      else cells.set(id, [point]);
    }
    this.references.push({
      map: mapKey(patch),
      position: [...patch.cameraPosition],
      forward: [...patch.cameraForward],
      cells,
    });
    if (this.references.length > MAX_REFERENCES) this.references.shift();
  }
}
