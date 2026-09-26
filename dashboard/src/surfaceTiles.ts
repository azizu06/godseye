import type { SurfacePatch } from "./surfaceTypes";
import type { CloudBounds } from "./pointCloud";

type Tile = {
  positions: number[];
  colors: number[];
  indices: number[];
  vertices: Map<string, number>;
  faces: Set<string>;
};

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
    const { positions: p, colors: c, indices } = patch;
    if (
      !c ||
      p.length !== c.length ||
      p.length % 3 ||
      indices.length % 3 ||
      !p.every((v) => Number.isFinite(v) && Math.abs(v) <= 1e6) ||
      !c.every((v) => Number.isFinite(v) && v >= 0 && v <= 1) ||
      !indices.every((i) => i < p.length / 3)
    )
      throw Error("Invalid observed surface");
    const dirty = new Set<string>();
    for (let i = 0; i < indices.length; i += 3) {
      const corners = [indices[i], indices[i + 1], indices[i + 2]];
      const id = [0, 1, 2]
        .map((axis) =>
          Math.floor(corners.reduce((s, v) => s + p[v * 3 + axis], 0) / 3),
        )
        .join(",");
      let tile = this.tiles.get(id);
      const keys = corners.map((v) =>
        [0, 1, 2].map((axis) => Math.round(p[v * 3 + axis] / 0.002)).join(","),
      );
      if (new Set(keys).size !== 3) continue;
      const mapped = keys.map((key) => tile?.vertices.get(key));
      const newCount = mapped.filter((v) => v === undefined).length;
      const existing =
        newCount === 0 &&
        tile!.faces.has([...mapped].sort((a, b) => a! - b!).join(","));
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
      const face: number[] = [];
      let changed = false;
      for (let j = 0; j < 3; j++) {
        let index = mapped[j];
        if (index === undefined) {
          index = tile.vertices.size;
          tile.vertices.set(keys[j], index);
          this.vertices++;
          changed = true;
        }
        face.push(index);
        for (let axis = 0; axis < 3; axis++) {
          const from = corners[j] * 3 + axis,
            to = index * 3 + axis;
          if (tile.positions[to] !== p[from] || tile.colors[to] !== c[from])
            changed = true;
          tile.positions[to] = p[from];
          tile.colors[to] = c[from];
          this.min[axis] = Math.min(this.min[axis], p[from]);
          this.max[axis] = Math.max(this.max[axis], p[from]);
        }
      }
      if (!existing) {
        tile.indices.push(...face);
        tile.faces.add([...face].sort((a, b) => a - b).join(","));
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
