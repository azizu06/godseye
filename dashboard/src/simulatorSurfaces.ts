import type { Vec3 } from "./protocol";
import type { SurfacePatch } from "./surfaceTypes";

type FurnitureBox = { position: Vec3; size: Vec3 };
// Coarse display cells trade detail for fast, connected observed coverage.
export const SIMULATED_SURFACE_CELL_M = 0.3;
const TRIANGLES_PER_SCAN = 3_600;
const linear = (channel: number) =>
  channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
const rgb = (hex: string) =>
  [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);

/** Hidden simulation geometry. Only indices accepted by SurfaceDiscovery may be rendered. */
export function makeRoomSurfaces(furniture: FurnitureBox[]): SurfacePatch {
  const positions: number[] = [],
    colors: number[] = [],
    indices: number[] = [];
  const face = (
    origin: Vec3,
    u: Vec3,
    v: Vec3,
    color: string,
    shade = 1,
    reverse = false,
    wood = false,
  ) => {
    const columns = Math.ceil(Math.hypot(...u) / SIMULATED_SURFACE_CELL_M),
      rows = Math.ceil(Math.hypot(...v) / SIMULATED_SURFACE_CELL_M);
    const base = positions.length / 3,
      tone = rgb(color);
    for (let row = 0; row <= rows; row++)
      for (let col = 0; col <= columns; col++) {
        const point = origin.map(
          (p, axis) => p + (u[axis] * col) / columns + (v[axis] * row) / rows,
        ) as Vec3;
        positions.push(...point);
        const grain = wood
          ? 0.025 * Math.sin(point[2] * 18 + Math.sin(point[0] * 4)) +
            0.025 * Math.sin(Math.floor(point[0] / 0.4) * 2.3)
          : 0;
        const variation =
          shade +
          grain +
          0.009 * Math.sin(point[0] * 9 + point[1] * 5 + point[2] * 7);
        colors.push(
          ...tone.map((c) => linear(Math.max(0, Math.min(1, c * variation)))),
        );
      }
    for (let row = 0; row < rows; row++)
      for (let col = 0; col < columns; col++) {
        const a = base + row * (columns + 1) + col,
          b = a + 1,
          d = a + columns + 1,
          c = d + 1;
        indices.push(...(reverse ? [a, c, b, a, d, c] : [a, b, c, a, c, d]));
      }
  };
  face([-4, 0, -3], [8, 0, 0], [0, 0, 6], "#a58b6e", 1, true, true);
  // Preserve the doorway in the existing hidden point-cloud scene.
  face([-4, 0, -3], [3.4, 0, 0], [0, 2.3, 0], "#c6c1b6");
  face([0.6, 0, -3], [3.4, 0, 0], [0, 2.3, 0], "#c6c1b6");
  face([-0.6, 1.8, -3], [1.2, 0, 0], [0, 0.5, 0], "#c6c1b6");
  face([4, 0, 3], [-8, 0, 0], [0, 2.3, 0], "#c9c4bb", 0.95);
  face([-4, 0, 3], [0, 0, -6], [0, 2.3, 0], "#c2beb4", 0.9);
  face([4, 0, -3], [0, 0, 6], [0, 2.3, 0], "#c2beb4", 0.94);
  const palette = [
    "#527771",
    "#527771",
    "#b18a60",
    "#424a47",
    "#a1815f",
    "#89755e",
  ];
  furniture.forEach((box, index) => {
    for (let axis = 0; axis < 3; axis++)
      for (const sign of [-1, 1]) {
        const b = (axis + 1) % 3,
          c = (axis + 2) % 3;
        const origin = box.position.map((p, a) => p - box.size[a] / 2) as Vec3;
        origin[axis] = box.position[axis] + (sign * box.size[axis]) / 2;
        const u: Vec3 = [0, 0, 0],
          v: Vec3 = [0, 0, 0];
        u[b] = box.size[b];
        v[c] = box.size[c];
        face(
          origin,
          u,
          v,
          palette[index % palette.length],
          axis === 1 && sign === 1 ? 1 : axis === 1 ? 0.65 : 0.86,
          sign === -1,
          index >= 2 && index !== 3,
        );
      }
  });
  return {
    id: "sim-observed-surfaces",
    positions: new Float32Array(positions),
    colors: new Float32Array(colors),
    indices: new Uint32Array(indices),
  };
}

/** A finite hidden scene with a monotonic, bounded set of observed triangles. */
export class SurfaceDiscovery {
  private readonly seen: Uint8Array;
  private readonly vertexVisibility: Int32Array;
  private readonly retained: number[] = [];
  private cursor = 0;
  private scanEpoch = 0;
  private dirty = false;
  private cached: SurfacePatch | null = null;
  constructor(private readonly geometry: SurfacePatch) {
    const count = geometry.indices.length / 3;
    this.seen = new Uint8Array(count);
    this.vertexVisibility = new Int32Array(geometry.positions.length / 3);
  }
  reset() {
    this.seen.fill(0);
    this.vertexVisibility.fill(0);
    this.retained.length = 0;
    this.cursor = this.scanEpoch = 0;
    this.dirty = false;
    this.cached = null;
  }
  scan(visible: (point: Vec3) => boolean) {
    const epoch = ++this.scanEpoch;
    const point = (index: number): Vec3 => {
      const offset = index * 3;
      return [
        this.geometry.positions[offset],
        this.geometry.positions[offset + 1],
        this.geometry.positions[offset + 2],
      ];
    };
    const vertexVisible = (index: number) => {
      const cached = this.vertexVisibility[index];
      if (Math.abs(cached) === epoch) return cached > 0;
      const result = visible(point(index));
      this.vertexVisibility[index] = result ? epoch : -epoch;
      return result;
    };
    const count = this.seen.length;
    for (
      let checked = 0;
      checked < Math.min(TRIANGLES_PER_SCAN, count);
      checked++
    ) {
      const triangle = this.cursor;
      // Adjacent triangles share their cell, avoiding scattered speckles.
      this.cursor = (this.cursor + 1) % count;
      if (this.seen[triangle]) continue;
      const offset = triangle * 3;
      const a = this.geometry.indices[offset],
        b = this.geometry.indices[offset + 1],
        c = this.geometry.indices[offset + 2];
      if (!vertexVisible(a) || !vertexVisible(b) || !vertexVisible(c)) continue;
      const points = [point(a), point(b), point(c)];
      const center = [0, 1, 2].map(
        (axis) => (points[0][axis] + points[1][axis] + points[2][axis]) / 3,
      ) as Vec3;
      if (!visible(center)) continue;
      if (
        [0, 1, 2].some(
          (i) =>
            !visible(
              points[i].map(
                (value, axis) => (value + points[(i + 1) % 3][axis]) / 2,
              ) as Vec3,
            ),
        )
      )
        continue;
      this.seen[triangle] = 1;
      this.retained.push(a, b, c);
      this.dirty = true;
    }
  }
  getSurface(): SurfacePatch | null {
    if (!this.retained.length) return null;
    if (this.dirty) {
      // Vertex/color buffers are immutable and shared. Replacing only the draw
      // indices keeps previous snapshots valid without rebuilding the room.
      this.cached = {
        ...this.geometry,
        indices: new Uint32Array(this.retained),
      };
      this.dirty = false;
    }
    return this.cached;
  }
}
