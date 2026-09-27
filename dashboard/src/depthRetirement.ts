/** Same-frame optical depth evidence; never retained with camera textures. */
export interface DepthObservation {
  sessionId: string;
  mapEpoch: number;
  capturedAt: number;
  width: number;
  height: number;
  depth: Float32Array;
  confidence: Uint8Array;
  projection: {
    transform: number[];
    intrinsics: number[];
    imageWidth: number;
    imageHeight: number;
  };
}
type Level = { width: number; height: number; values: Float32Array };
/** Invalid confidence/depth contributes zero, so every covered pixel must prove free space. */
class DepthMinimum {
  private levels: Level[] = [];
  constructor(readonly observation: DepthObservation) {
    const { width, height, depth, confidence } = observation;
    const values = new Float32Array(width * height);
    for (let i = 0; i < values.length; i++)
      if (
        confidence[i] === 2 &&
        Number.isFinite(depth[i]) &&
        depth[i] >= 0.05 &&
        depth[i] <= 5
      )
        values[i] = depth[i];
    this.levels.push({ width, height, values });
    while (this.levels.at(-1)!.width > 1 || this.levels.at(-1)!.height > 1) {
      const prior = this.levels.at(-1)!;
      const w = Math.ceil(prior.width / 2),
        h = Math.ceil(prior.height / 2);
      const next = new Float32Array(w * h).fill(Infinity);
      for (let y = 0; y < prior.height; y++)
        for (let x = 0; x < prior.width; x++) {
          const i = Math.floor(y / 2) * w + Math.floor(x / 2);
          next[i] = Math.min(next[i], prior.values[y * prior.width + x]);
        }
      this.levels.push({ width: w, height: h, values: next });
    }
  }
  private allFurther(
    left: number,
    top: number,
    right: number,
    bottom: number,
    threshold: number,
  ): boolean {
    // Most projected samples occupy a few native pixels; avoid hierarchy overhead.
    if ((right - left + 1) * (bottom - top + 1) <= 64) {
      const base = this.levels[0];
      for (let y = top; y <= bottom; y++)
        for (let x = left; x <= right; x++)
          if (base.values[y * base.width + x] <= threshold) return false;
      return true;
    }
    const visit = (level: number, x: number, y: number): boolean => {
      const size = 2 ** level,
        x0 = x * size,
        y0 = y * size;
      if (x0 > right || y0 > bottom || x0 + size <= left || y0 + size <= top)
        return true;
      const grid = this.levels[level];
      if (x >= grid.width || y >= grid.height) return true;
      // A minimum farther than the old face proves even a partially overlapping node.
      if (grid.values[y * grid.width + x] > threshold) return true;
      if (
        !level ||
        (x0 >= left &&
          y0 >= top &&
          x0 + size - 1 <= right &&
          y0 + size - 1 <= bottom)
      )
        return false;
      return (
        visit(level - 1, x * 2, y * 2) &&
        visit(level - 1, x * 2 + 1, y * 2) &&
        visit(level - 1, x * 2, y * 2 + 1) &&
        visit(level - 1, x * 2 + 1, y * 2 + 1)
      );
    };
    return visit(this.levels.length - 1, 0, 0);
  }
  contradicts(points: readonly ArrayLike<number>[]): boolean {
    const { width, height, projection } = this.observation;
    const {
      transform: t,
      intrinsics: k,
      imageWidth: iw,
      imageHeight: ih,
    } = projection;
    let left = Infinity,
      top = Infinity,
      right = -Infinity,
      bottom = -Infinity,
      farthest = 0;
    for (const point of points) {
      const dx = point[0] - t[12],
        dy = point[1] - t[13],
        dz = point[2] - t[14];
      const x = dx * t[0] + dy * t[1] + dz * t[2];
      const y = dx * t[4] + dy * t[5] + dz * t[6];
      const depth = -(dx * t[8] + dy * t[9] + dz * t[10]);
      if (!Number.isFinite(depth) || depth < 0.05 || depth > 5) return false;
      const u = (((k[0] * x) / depth + k[6]) * width) / iw - 0.5;
      const v = (((-k[4] * y) / depth + k[7]) * height) / ih - 0.5;
      if (!Number.isFinite(u) || !Number.isFinite(v)) return false;
      left = Math.min(left, u);
      right = Math.max(right, u);
      top = Math.min(top, v);
      bottom = Math.max(bottom, v);
      farthest = Math.max(farthest, depth);
    }
    // Full padded bounding rectangle is stronger than corner/centroid sampling:
    // any hole, silhouette, or closer occluder anywhere inside retains the face.
    left = Math.floor(left) - 1;
    right = Math.ceil(right) + 1;
    top = Math.floor(top) - 1;
    bottom = Math.ceil(bottom) + 1;
    if (left < 0 || top < 0 || right >= width || bottom >= height) return false;
    return this.allFurther(
      left,
      top,
      right,
      bottom,
      farthest + Math.max(0.12, farthest * 0.05),
    );
  }
}
function validProjection(o: DepthObservation): boolean {
  const {
    transform: t,
    intrinsics: k,
    imageWidth,
    imageHeight,
  } = o.projection ?? {};
  if (
    !t ||
    !k ||
    t.length !== 16 ||
    k.length !== 9 ||
    ![...t, ...k, imageWidth, imageHeight].every(Number.isFinite) ||
    imageWidth <= 0 ||
    imageHeight <= 0 ||
    k[0] <= 0 ||
    k[4] <= 0 ||
    [t[3], t[7], t[11], t[15] - 1].some((v) => Math.abs(v) > 0.0001)
  )
    return false;
  for (let a = 0; a < 3; a++)
    for (let b = 0; b < 3; b++)
      if (
        Math.abs(
          t[a * 4] * t[b * 4] +
            t[a * 4 + 1] * t[b * 4 + 1] +
            t[a * 4 + 2] * t[b * 4 + 2] -
            (a === b ? 1 : 0),
        ) > 0.002
      )
        return false;
  const det =
    t[0] * (t[5] * t[10] - t[9] * t[6]) -
    t[4] * (t[1] * t[10] - t[9] * t[2]) +
    t[8] * (t[1] * t[6] - t[5] * t[2]);
  return Math.abs(det - 1) <= 0.002;
}
const mapKey = (o: DepthObservation) =>
  JSON.stringify([o.sessionId, o.mapEpoch]);
/** Two distinct calibrated observations must both contradict the complete old footprint. */
export class DepthContradiction {
  readonly mapKey: string;
  readonly before: number;
  readonly capturedAt: number;
  private views: DepthMinimum[];
  constructor(observations: readonly DepthObservation[]) {
    const first = observations[0],
      last = observations[1];
    if (
      observations.length !== 2 ||
      !first ||
      !last ||
      mapKey(first) !== mapKey(last) ||
      !Number.isFinite(first.capturedAt) ||
      !Number.isFinite(last.capturedAt) ||
      first.capturedAt >= last.capturedAt ||
      observations.some(
        (o) =>
          !validProjection(o) ||
          !Number.isSafeInteger(o.width) ||
          !Number.isSafeInteger(o.height) ||
          o.width < 1 ||
          o.height < 1 ||
          o.width * o.height > 65536 ||
          o.depth.length !== o.width * o.height ||
          o.confidence.length !== o.depth.length,
      )
    )
      throw Error("Invalid depth retirement evidence");
    this.mapKey = mapKey(first);
    this.before = first.capturedAt;
    this.capturedAt = last.capturedAt;
    this.views = observations.map((o) => new DepthMinimum(o));
  }
  point(x: number, y: number, z: number): boolean {
    return this.views.every((view) => view.contradicts([[x, y, z]]));
  }
  triangle(
    a: ArrayLike<number>,
    b: ArrayLike<number>,
    c: ArrayLike<number>,
  ): boolean {
    return this.views.every((view) => view.contradicts([a, b, c]));
  }
  /** A remembered volume (its corner points) is seen through in both views. */
  region(points: readonly ArrayLike<number>[]): boolean {
    return (
      points.length > 0 && this.views.every((view) => view.contradicts(points))
    );
  }
}
