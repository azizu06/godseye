import type { SurfaceTileUpdate } from "./surfaceTypes";
import type { CloudBounds } from "./pointCloud";
import { MAX_SURFACE_TRIANGLES, MAX_SURFACE_VERTICES } from "./surfaceLimits";

export type UploadRange = { start: number; count: number };
export function mergeUploadRanges(ranges: UploadRange[]) {
  ranges.sort((a, b) => a.start - b.start);
  const result: UploadRange[] = [];
  for (const range of ranges) {
    const last = result.at(-1);
    if (last && range.start <= last.start + last.count)
      last.count = Math.max(last.count, range.start + range.count - last.start);
    else result.push({ ...range });
  }
  return result;
}
function capacity(current: number, required: number, maximum: number) {
  let next = Math.max(64, current);
  while (next < required) next = Math.min(maximum, next * 2);
  return Math.min(maximum, next);
}

/** Main-thread mirror; stable arrays and bounded dirty coverage between renders. */
export class SurfaceTileBuffer {
  positions = new Float32Array(0);
  colors = new Float32Array(0);
  indices = new Uint32Array(0);
  vertexCount = 0;
  indexCount = 0;
  revision = 0;
  bounds: CloudBounds = { center: [0, 0, 0], radius: 0 };
  private vertices: UploadRange[] = [];
  private topology: UploadRange[] = [];
  constructor(readonly id: string) {}

  apply(update: SurfaceTileUpdate) {
    const { vertexCount, indexCount, indexStart, spans } = update;
    if (
      update.id !== this.id ||
      update.revision !== this.revision + 1 ||
      !Number.isSafeInteger(vertexCount) ||
      vertexCount < this.vertexCount ||
      vertexCount > MAX_SURFACE_VERTICES ||
      !Number.isSafeInteger(indexCount) ||
      indexCount < this.indexCount ||
      indexCount > MAX_SURFACE_TRIANGLES * 3 ||
      indexCount % 3 ||
      indexStart !== this.indexCount ||
      indexCount !== indexStart + update.indices.length ||
      spans.length % 2 ||
      update.positions.length !== update.colors.length ||
      !update.indices.every((i) => i < vertexCount)
    )
      throw Error("Invalid surface tile delta");
    let total = 0,
      end = 0,
      appended = 0;
    const ranges: UploadRange[] = [];
    for (let i = 0; i < spans.length; i += 2) {
      const start = spans[i],
        count = spans[i + 1];
      if (
        start % 3 ||
        count % 3 ||
        count === 0 ||
        start < end ||
        start + count > vertexCount * 3
      )
        throw Error("Invalid surface vertex range");
      end = start + count;
      appended += Math.max(0, end - Math.max(start, this.vertexCount * 3));
      total += count;
      ranges.push({ start, count });
    }
    if (
      total !== update.positions.length ||
      appended !== (vertexCount - this.vertexCount) * 3
    )
      throw Error("Incomplete surface tile delta");
    // Validate before changing the displayed scan, including on buffer growth.
    if (vertexCount * 3 > this.positions.length) {
      const size =
        capacity(this.positions.length / 3, vertexCount, MAX_SURFACE_VERTICES) *
        3;
      const positions = new Float32Array(size),
        colors = new Float32Array(size);
      positions.set(this.positions);
      colors.set(this.colors);
      this.positions = positions;
      this.colors = colors;
    }
    if (indexCount > this.indices.length) {
      const indices = new Uint32Array(
        capacity(this.indices.length, indexCount, MAX_SURFACE_TRIANGLES * 3),
      );
      indices.set(this.indices);
      this.indices = indices;
    }
    let offset = 0;
    for (const { start, count } of ranges) {
      this.positions.set(
        update.positions.subarray(offset, offset + count),
        start,
      );
      this.colors.set(update.colors.subarray(offset, offset + count), start);
      offset += count;
    }
    this.indices.set(update.indices, indexStart);
    this.vertices = mergeUploadRanges([...this.vertices, ...ranges]);
    if (update.indices.length)
      this.topology = mergeUploadRanges([
        ...this.topology,
        { start: indexStart, count: update.indices.length },
      ]);
    this.vertexCount = vertexCount;
    this.indexCount = indexCount;
    this.revision = update.revision;
    this.bounds = update.bounds;
  }

  takeUploadRanges() {
    const ranges = { vertices: this.vertices, indices: this.topology };
    this.vertices = [];
    this.topology = [];
    return ranges;
  }
}

export type RetainedSurfaceTile = {
  buffer: SurfaceTileBuffer;
  revision: number;
};
