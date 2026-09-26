import type { CapturedSurface } from "./surfaceTypes";

const CELL = 0.025;
const REQUIRED_OBSERVATIONS = 3;
const PLANE_TOLERANCE = 0.008;
const NORMAL_AGREEMENT = 0.98;
const MAX_GAP_SECONDS = 15;
type Evidence = {
  x: number;
  y: number;
  z: number;
  nx: number;
  ny: number;
  nz: number;
  observations: number;
  frame: number;
  time: number;
};

/** Confirm local measured planes; never snap or average the rendered coordinates. */
export class SurfaceEvidence {
  private cells = new Map<string, Evidence>();
  private identity = "";
  private latestTime = -1;
  private latestFrame = -1;
  constructor(private readonly capacity = 200_000) {}
  get size() {
    return this.cells.size;
  }

  confirm(surface: CapturedSurface, support?: Uint8Array): Uint32Array {
    const identity = JSON.stringify([surface.sessionId, surface.mapEpoch]);
    if (identity !== this.identity) {
      this.cells.clear();
      this.latestTime = this.latestFrame = -1;
      this.identity = identity;
    }
    // Re-reading a cached frame cannot manufacture temporal evidence.
    if (
      surface.capturedAt <= this.latestTime ||
      surface.frameId <= this.latestFrame
    )
      return new Uint32Array();
    this.latestTime = surface.capturedAt;
    this.latestFrame = surface.frameId;
    const { positions: p, indices } = surface;
    const confirmed = new Uint32Array(indices.length);
    let count = 0;
    for (let i = 0; i < indices.length; i += 3) {
      // Decoder already requires valid neighboring depth and bounds every
      // coarse cell's error. Eight same-frame native samples provide enough
      // spatial support without waiting for an arbitrary number of revisits.
      if ((support?.[i / 3] ?? 0) >= 8) {
        confirmed[count++] = indices[i];
        confirmed[count++] = indices[i + 1];
        confirmed[count++] = indices[i + 2];
        continue;
      }
      const a = indices[i] * 3,
        b = indices[i + 1] * 3,
        c = indices[i + 2] * 3;
      const ux = p[b] - p[a],
        uy = p[b + 1] - p[a + 1],
        uz = p[b + 2] - p[a + 2];
      const vx = p[c] - p[a],
        vy = p[c + 1] - p[a + 1],
        vz = p[c + 2] - p[a + 2];
      let nx = uy * vz - uz * vy,
        ny = uz * vx - ux * vz,
        nz = ux * vy - uy * vx;
      const length = Math.hypot(nx, ny, nz);
      if (length < 1e-10) continue;
      nx /= length;
      ny /= length;
      nz /= length;
      const x = (p[a] + p[b] + p[c]) / 3;
      const y = (p[a + 1] + p[b + 1] + p[c + 1]) / 3;
      const z = (p[a + 2] + p[b + 2] + p[c + 2]) / 3;
      const axis =
        Math.abs(nx) > Math.abs(ny)
          ? Math.abs(nx) > Math.abs(nz)
            ? 0
            : 2
          : Math.abs(ny) > Math.abs(nz)
            ? 1
            : 2;
      const key = `${Math.round(x / CELL)},${Math.round(y / CELL)},${Math.round(z / CELL)},${axis}`;
      let evidence = this.cells.get(key);
      const touched = evidence?.frame !== surface.frameId;
      const matches =
        evidence &&
        surface.capturedAt - evidence.time <= MAX_GAP_SECONDS &&
        Math.abs(nx * evidence.nx + ny * evidence.ny + nz * evidence.nz) >=
          NORMAL_AGREEMENT &&
        Math.abs(
          (x - evidence.x) * evidence.nx +
            (y - evidence.y) * evidence.ny +
            (z - evidence.z) * evidence.nz,
        ) <= PLANE_TOLERANCE;
      if (!matches) {
        // Conflicting triangles in one frame cannot overwrite its first evidence
        // and count as another observation. They remain points.
        if (evidence?.frame === surface.frameId) continue;
        evidence = {
          x,
          y,
          z,
          nx,
          ny,
          nz,
          observations: 1,
          frame: surface.frameId,
          time: surface.capturedAt,
        };
      } else if (evidence!.frame !== surface.frameId) {
        evidence!.observations = Math.min(
          REQUIRED_OBSERVATIONS,
          evidence!.observations + 1,
        );
        evidence!.frame = surface.frameId;
        evidence!.time = surface.capturedAt;
      }
      if (touched) {
        this.cells.delete(key);
        this.cells.set(key, evidence!);
      }
      if (this.cells.size > this.capacity)
        this.cells.delete(this.cells.keys().next().value!);
      if (evidence!.observations >= REQUIRED_OBSERVATIONS) {
        confirmed[count++] = indices[i];
        confirmed[count++] = indices[i + 1];
        confirmed[count++] = indices[i + 2];
      }
    }
    return confirmed.slice(0, count);
  }
}
