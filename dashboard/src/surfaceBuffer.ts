import type { SurfacePatch } from "./surfaceTypes";

export interface SurfaceDelta {
  revision: number;
  reset: boolean;
  vertexStart: number;
  indexStart: number;
  vertexCount: number;
  indexCount: number;
  positions: Float32Array;
  colors: Float32Array;
  indices: Uint32Array;
}

/**
 * Stable storage between worker appends. Coarsening replaces all storage; a
 * face deletion replaces only the index tail it changed.
 */
export class SurfaceBuffer {
  private positions = new Float32Array(0);
  private colors = new Float32Array(0);
  private indices = new Uint32Array(0);
  private patch: SurfacePatch | null = null;
  apply(delta: SurfaceDelta): SurfacePatch | null {
    if (this.patch?.update?.revision === delta.revision && !delta.reset)
      return this.patch;
    if (
      delta.vertexCount > 500_000 ||
      delta.indexCount > 3_000_000 ||
      delta.positions.length !== (delta.vertexCount - delta.vertexStart) * 3 ||
      delta.colors.length !== delta.positions.length ||
      delta.indices.length !== delta.indexCount - delta.indexStart
    )
      throw Error("Invalid surface delta");
    // A cancelled request still drains worker deltas. Keep already published
    // views intact until a replacement topology is published atomically.
    if (delta.reset && this.patch) {
      this.positions = new Float32Array(this.positions.length);
      this.colors = new Float32Array(this.colors.length);
      this.indices = new Uint32Array(this.indices.length);
    } else if (this.patch && delta.indexStart < this.patch.indices.length) {
      // Face deletion rewrites topology from its first removed face. Vertex
      // storage is unchanged, so only indices are copied (same capacity, so the
      // renderer can reuse its GPU buffers and upload just the rewritten range).
      const indices = new Uint32Array(this.indices.length);
      indices.set(this.indices.subarray(0, delta.indexStart));
      this.indices = indices;
    }
    if (delta.vertexCount * 3 > this.positions.length) {
      const size = Math.min(
        1_500_000,
        Math.max(delta.vertexCount * 3, this.positions.length * 2, 12_288),
      );
      const positions = new Float32Array(size),
        colors = new Float32Array(size);
      positions.set(this.positions);
      colors.set(this.colors);
      this.positions = positions;
      this.colors = colors;
    }
    if (delta.indexCount > this.indices.length) {
      const indices = new Uint32Array(
        Math.min(
          3_000_000,
          Math.max(delta.indexCount, this.indices.length * 2, 24_576),
        ),
      );
      indices.set(this.indices);
      this.indices = indices;
    }
    this.positions.set(delta.positions, delta.vertexStart * 3);
    this.colors.set(delta.colors, delta.vertexStart * 3);
    this.indices.set(delta.indices, delta.indexStart);
    this.patch = delta.indexCount
      ? {
          id: "persistent-colored-map",
          positions: this.positions.subarray(0, delta.vertexCount * 3),
          colors: this.colors.subarray(0, delta.vertexCount * 3),
          indices: this.indices.subarray(0, delta.indexCount),
          update: {
            revision: delta.revision,
            vertexStart: delta.vertexStart,
            indexStart: delta.indexStart,
            positions: this.positions,
            colors: this.colors,
            indices: this.indices,
          },
        }
      : null;
    return this.patch;
  }
}
