import type { SurfacePatch } from "./surfaceTypes";
import type { CloudBounds } from "./pointCloud";

type Tile = {
  positions: number[];
  colors: number[];
  indices: number[];
  vertices: Map<string, number>;
  faces: Set<string>;
};

function faceKey(a: number, b: number, c: number) {
  // Canonicalize a triangle without allocating/sorting an array per observation.
  if (a > b) [a, b] = [b, a];
  if (b > c) [b, c] = [c, b];
  if (a > b) [a, b] = [b, a];
  return `${a},${b},${c}`;
}

/** Accumulate observed triangles, updating only touched one-meter tiles.
 * No room planes, inferred faces, global remeshing, or eviction of distant scans.
 */
export class SurfaceTiles {
  private tiles = new Map<string, Tile>();
  private vertices = 0;
  private min = [Infinity, Infinity, Infinity];
  private max = [-Infinity, -Infinity, -Infinity];
  triangles = 0;
  capacity = false;
  constructor(
    private maxTriangles = 500_000,
    private maxVertices = 500_000,
  ) {}

  bounds(): CloudBounds | null {
    if (!this.triangles) return null;
    return {
      center: this.min.map((v, i) => (v + this.max[i]) / 2) as [
        number,
        number,
        number,
      ],
      radius: Math.max(
        0.1,
        Math.hypot(...this.max.map((v, i) => v - this.min[i])) / 2,
      ),
    };
  }

  add(patch: SurfacePatch): SurfacePatch[] {
    const { positions: p, colors, indices } = patch;
    if (
      !colors ||
      p.length !== colors.length ||
      p.length % 3 ||
      indices.length % 3 ||
      !p.every((v) => Number.isFinite(v) && Math.abs(v) <= 1e6) ||
      !colors.every((v) => Number.isFinite(v) && v >= 0 && v <= 1) ||
      !indices.every((i) => i < p.length / 3)
    )
      throw Error("Invalid observed surface");
    const dirty = new Set<string>();
    // Each measured vertex participates in several faces. Calculate its spatial
    // key once per frame while retaining the original measured coordinates.
    const keys = new Array<string>(p.length / 3);
    const keyAt = (v: number) =>
      (keys[v] ??=
        `${Math.round(p[v * 3] / 0.002)},${Math.round(p[v * 3 + 1] / 0.002)},${Math.round(p[v * 3 + 2] / 0.002)}`);
    const corners = [0, 0, 0];
    const mapped: (number | undefined)[] = [0, 0, 0];
    const face = [0, 0, 0];
    for (let i = 0; i < indices.length; i += 3) {
      const a = indices[i],
        b = indices[i + 1],
        c = indices[i + 2];
      corners[0] = a;
      corners[1] = b;
      corners[2] = c;
      const id = `${Math.floor((p[a * 3] + p[b * 3] + p[c * 3]) / 3)},${Math.floor((p[a * 3 + 1] + p[b * 3 + 1] + p[c * 3 + 1]) / 3)},${Math.floor((p[a * 3 + 2] + p[b * 3 + 2] + p[c * 3 + 2]) / 3)}`;
      let tile = this.tiles.get(id);
      const ka = keyAt(a),
        kb = keyAt(b),
        kc = keyAt(c);
      if (ka === kb || ka === kc || kb === kc) continue;
      mapped[0] = tile?.vertices.get(ka);
      mapped[1] = tile?.vertices.get(kb);
      mapped[2] = tile?.vertices.get(kc);
      const newCount =
        Number(mapped[0] === undefined) +
        Number(mapped[1] === undefined) +
        Number(mapped[2] === undefined);
      const existing =
        newCount === 0 &&
        tile!.faces.has(faceKey(mapped[0]!, mapped[1]!, mapped[2]!));
      if (
        !existing &&
        (this.triangles >= this.maxTriangles ||
          this.vertices + newCount > this.maxVertices ||
          (!tile && this.tiles.size >= 2048))
      ) {
        this.capacity = true;
        continue;
      }
      if (!tile) {
        tile = {
          positions: [],
          colors: [],
          indices: [],
          vertices: new Map(),
          faces: new Set(),
        };
        this.tiles.set(id, tile);
      }
      let changed = false;
      for (let j = 0; j < 3; j++) {
        let index = mapped[j];
        if (index === undefined) {
          index = tile.vertices.size;
          tile.vertices.set(keys[corners[j]], index);
          this.vertices++;
          changed = true;
        }
        face[j] = index;
        for (let axis = 0; axis < 3; axis++) {
          const from = corners[j] * 3 + axis,
            to = index * 3 + axis;
          if (
            tile.positions[to] !== p[from] ||
            tile.colors[to] !== colors[from]
          )
            changed = true;
          tile.positions[to] = p[from];
          tile.colors[to] = colors[from];
          this.min[axis] = Math.min(this.min[axis], p[from]);
          this.max[axis] = Math.max(this.max[axis], p[from]);
        }
      }
      if (!existing) {
        tile.indices.push(...face);
        tile.faces.add(faceKey(face[0], face[1], face[2]));
        this.triangles++;
        changed = true;
      }
      if (changed) dirty.add(id);
    }
    return [...dirty].map((id) => {
      const tile = this.tiles.get(id)!;
      return {
        id,
        positions: new Float32Array(tile.positions),
        colors: new Float32Array(tile.colors),
        indices: new Uint32Array(tile.indices),
      };
    });
  }
}
