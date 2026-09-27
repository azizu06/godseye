import { DepthContradiction, type DepthObservation } from "./depthRetirement";
export const MAX_POINT_RETIREMENTS = 100_000;
export const POINT_RETIREMENT_VISITS = 100_000;
export const MAX_POINTS = 2_000_000;
export const VOXEL_SIZE = 0.01;
export const MAX_CHUNK_POINTS = 2500;
export const MAX_DENSE_POINTS = 20_000;
export const POINTS_PROTOCOL = "godseye.points.v2";

type Identity = { session_id: string; map_epoch: number };
export type PointChunk = {
  version: 1 | 2;
  type: "points";
  chunk_id: number;
  positions: number[] | Float32Array;
  colors: number[] | Uint8Array | Float32Array;
  session_id?: string;
  map_epoch?: number;
  t_capture?: number;
};
export type CapturedPoints = {
  sessionId: string;
  mapEpoch: number;
  frameId: number;
  capturedAt: number;
  positions: Float32Array;
  colors: Float32Array; // Already linear, sampled from the calibrated camera image.
  retirement?: DepthObservation[]; // Two calibrated observations, distinct from coverage culling.
  covered?: Uint8Array; // Same-frame measured samples replaced by retained triangles.
};
export type CloudBounds = { center: [number, number, number]; radius: number };

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const integer = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

function identity(value: Record<string, unknown>): Identity | null {
  return typeof value.session_id === "string" &&
    value.session_id.length > 0 &&
    value.session_id.length <= 256 &&
    integer(value.map_epoch)
    ? { session_id: value.session_id, map_epoch: value.map_epoch }
    : null;
}
const keyFor = (value: Identity | null) =>
  value ? JSON.stringify([value.session_id, value.map_epoch]) : "legacy";

export function parsePointChunk(value: unknown): PointChunk | null {
  if (value instanceof ArrayBuffer) return parseBinaryPoints(value);
  if (
    !record(value) ||
    value.version !== 1 ||
    value.type !== "points" ||
    !integer(value.chunk_id)
  )
    return null;
  const { positions, colors } = value;
  if (
    !Array.isArray(positions) ||
    !Array.isArray(colors) ||
    positions.length === 0 ||
    positions.length % 3 !== 0 ||
    positions.length > MAX_CHUNK_POINTS * 3 ||
    colors.length !== positions.length
  )
    return null;
  if (
    !positions.every(
      (v) =>
        typeof v === "number" && Number.isFinite(v) && Math.abs(v) <= 1_000_000,
    ) ||
    !colors.every(
      (v) => typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= 1,
    )
  )
    return null;
  if (("session_id" in value || "map_epoch" in value) && !identity(value))
    return null;
  if (
    "t_capture" in value &&
    (typeof value.t_capture !== "number" ||
      !Number.isFinite(value.t_capture) ||
      value.t_capture < 0)
  )
    return null;
  return value as PointChunk;
}

export function parseBinaryPoints(data: ArrayBuffer): PointChunk | null {
  if (data.byteLength < 8 || data.byteLength > 4100 + MAX_DENSE_POINTS * 15)
    return null;
  const length = new DataView(data).getUint32(0, true);
  if (!length || length > 4096 || length % 4 || 4 + length > data.byteLength)
    return null;
  try {
    const header: unknown = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(
        new Uint8Array(data, 4, length),
      ),
    );
    if (
      !record(header) ||
      header.version !== 2 ||
      header.type !== "points" ||
      !identity(header) ||
      !integer(header.chunk_id) ||
      !integer(header.frame_id) ||
      !integer(header.count) ||
      header.count < 1 ||
      header.count > MAX_DENSE_POINTS ||
      typeof header.t_capture !== "number" ||
      !Number.isFinite(header.t_capture) ||
      header.t_capture < 0 ||
      header.positions !== "float32_le" ||
      header.colors !== "rgb8_srgb" ||
      data.byteLength !== 4 + length + header.count * 15
    )
      return null;
    const positions = new Float32Array(data, 4 + length, header.count * 3);
    if (!positions.every((v) => Number.isFinite(v) && Math.abs(v) <= 1_000_000))
      return null;
    return {
      ...header,
      positions,
      colors: new Uint8Array(
        data,
        4 + length + header.count * 12,
        header.count * 3,
      ),
    } as PointChunk;
  } catch {
    return null;
  }
}

/** JPEG RGB is sRGB; Three.js vertex colors use linear RGB. */
export function linearColor(value: number) {
  return value <= 0.04045
    ? value / 12.92
    : Math.pow((value + 0.055) / 1.055, 2.4);
}

const rgb8Linear = Float32Array.from({ length: 256 }, (_, i) =>
  linearColor(i / 255),
);
type VoxelKey = number | string;
function voxelKey(x: number, y: number, z: number, size: number): VoxelKey {
  const a = Math.floor(x / size) + 65536;
  const b = Math.floor(y / size) + 65536;
  const c = Math.floor(z / size) + 65536;
  // Three 17-bit coordinates fit exactly in a JS number. Far-away samples use
  // the unbounded string path, never a colliding hash or wrapped coordinate.
  return a >= 0 && a < 131072 && b >= 0 && b < 131072 && c >= 0 && c < 131072
    ? (a * 131072 + b) * 131072 + c
    : `${a},${b},${c}`;
}

export type CloudUpdate = {
  count: number;
  evicted: number;
  retirementCapacity: boolean;
  retiredVoxelCount: number;
  spans: Uint32Array; // pairs of starting component and component count
  positions: Float32Array;
  colors: Float32Array;
  visibleCount: number;
  visibleSpans: Uint32Array;
  visibleIndices: Uint32Array;
};

function mergeRanges(ranges: { start: number; count: number }[]) {
  ranges.sort((a, b) => a.start - b.start);
  const merged: typeof ranges = [];
  for (const range of ranges) {
    const last = merged.at(-1);
    if (last && range.start <= last.start + last.count)
      last.count = Math.max(last.count, range.start + range.count - last.start);
    else merged.push({ ...range });
  }
  return merged;
}

/** Bounded spatial cache. Retain measured positions, never snap them to voxel centers. */
export class PointCloudStore {
  readonly positions: Float32Array;
  readonly colors: Float32Array;
  readonly visibleIndices: Uint32Array;
  visibleCount = 0;
  private visibleSlots?: Uint32Array; // worker only: packed-list offset + 1, or hidden
  private visibleDirty = new Set<number>();
  private visibleUploads: { start: number; count: number }[] = [];
  count = 0;
  evicted = 0;
  retirementCapacity = false;
  retiredVoxelCount = 0;
  private dirty = new Set<number>();
  private uploadRanges: { start: number; count: number }[] = [];
  private cursor = 0;
  private revision = 0;
  private mapKey: string | null = null;
  private retired = new Set<string>();
  private lastChunk = -1;
  private lastCapture = -1;
  private lastMeasured = -1;
  private observedAt?: Float64Array;
  private retirementCursor = 0;
  private recentRetirementSlots = new Uint32Array(1024);
  private recentRetirementCursor = 0;
  private recentRetirementCount = 0;
  private lastRetirement = -1;
  private retiredVoxels = new Map<VoxelKey, number>();
  private slots = new Map<VoxelKey, number>();
  private keys: (VoxelKey | undefined)[] = [];
  private listeners = new Set<() => void>();

  constructor(
    readonly capacity = MAX_POINTS,
    readonly voxelSize = VOXEL_SIZE,
  ) {
    if (
      !Number.isSafeInteger(capacity) ||
      capacity < 1 ||
      capacity > MAX_POINTS ||
      !Number.isFinite(voxelSize) ||
      voxelSize <= 0
    )
      throw Error("Invalid point cache limits");
    this.positions = new Float32Array(capacity * 3);
    this.colors = new Float32Array(capacity * 3);
    this.visibleIndices = new Uint32Array(capacity);
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  snapshot = () => this.revision;
  private changed() {
    this.revision++;
    this.listeners.forEach((listener) => listener());
  }
  private empty() {
    this.count = 0;
    this.visibleCount = 0;
    this.visibleSlots?.fill(0);
    this.visibleDirty.clear();
    this.visibleUploads = [];
    this.cursor = 0;
    this.evicted = 0;
    this.lastChunk = -1;
    this.lastCapture = -1;
    this.lastMeasured = -1;
    this.retirementCursor = 0;
    this.recentRetirementCursor = this.recentRetirementCount = 0;
    this.lastRetirement = -1;
    this.retiredVoxels.clear();
    this.retirementCapacity = false;
    this.retiredVoxelCount = 0;
    this.dirty.clear();
    this.uploadRanges = [];
    this.slots.clear();
    this.keys.length = 0;
  }
  reconnect() {
    this.lastChunk = this.lastCapture = -1;
  }
  restoreCoverage() {
    for (let i = 0; i < this.count; i++) this.setVisible(i, true);
    this.changed();
  }
  clear() {
    this.empty();
    this.mapKey = null;
    this.retired.clear();
    this.changed();
  }
  /** GPU uploads only touched ranges. Merge nearby slots to avoid tiny writes. */
  takeUpdateRanges() {
    const slots = [...this.dirty].sort((a, b) => a - b);
    this.dirty.clear();
    const ranges: { start: number; count: number }[] = [];
    for (const slot of slots) {
      const last = ranges.at(-1);
      if (last && slot * 3 <= last.start + last.count + 96)
        last.count = slot * 3 + 3 - last.start;
      else ranges.push({ start: slot * 3, count: 3 });
    }
    const uploads = this.uploadRanges;
    this.uploadRanges = [];
    return uploads.length ? mergeRanges([...uploads, ...ranges]) : ranges;
  }
  /** Worker -> renderer: transfer only changed ranges, never the full 2M cache. */
  takeUpdate(): CloudUpdate {
    const ranges = this.takeUpdateRanges();
    const size = ranges.reduce((sum, range) => sum + range.count, 0);
    const positions = new Float32Array(size),
      colors = new Float32Array(size);
    const spans = new Uint32Array(ranges.length * 2);
    let offset = 0;
    ranges.forEach(({ start, count }, i) => {
      spans.set([start, count], i * 2);
      positions.set(this.positions.subarray(start, start + count), offset);
      colors.set(this.colors.subarray(start, start + count), offset);
      offset += count;
    });
    const visibleRanges = this.takeVisibleRanges();
    const visibleSpans = new Uint32Array(visibleRanges.length * 2);
    const visibleIndices = new Uint32Array(
      visibleRanges.reduce((sum, r) => sum + r.count, 0),
    );
    offset = 0;
    visibleRanges.forEach(({ start, count }, i) => {
      visibleSpans.set([start, count], i * 2);
      visibleIndices.set(
        this.visibleIndices.subarray(start, start + count),
        offset,
      );
      offset += count;
    });
    return {
      count: this.count,
      evicted: this.evicted,
      retirementCapacity: this.retirementCapacity,
      retiredVoxelCount: this.retiredVoxelCount,
      spans,
      positions,
      colors,
      visibleCount: this.visibleCount,
      visibleSpans,
      visibleIndices,
    };
  }
  applyUpdate(update: CloudUpdate) {
    let offset = 0;
    for (let i = 0; i < update.spans.length; i += 2) {
      const start = update.spans[i],
        count = update.spans[i + 1];
      this.positions.set(
        update.positions.subarray(offset, offset + count),
        start,
      );
      this.colors.set(update.colors.subarray(offset, offset + count), start);
      this.uploadRanges.push({ start, count });
      offset += count;
    }
    this.count = update.count;
    offset = 0;
    for (let i = 0; i < update.visibleSpans.length; i += 2) {
      const start = update.visibleSpans[i],
        count = update.visibleSpans[i + 1];
      this.visibleIndices.set(
        update.visibleIndices.subarray(offset, offset + count),
        start,
      );
      this.visibleUploads.push({ start, count });
      offset += count;
    }
    this.visibleCount = update.visibleCount;
    if (this.visibleUploads.length > 256)
      this.visibleUploads = mergeRanges(this.visibleUploads);
    this.evicted = update.evicted;
    this.retirementCapacity = update.retirementCapacity;
    this.retiredVoxelCount = update.retiredVoxelCount;
    // Hidden tabs may receive points while animation frames are paused. Keep
    // their upload backlog bounded by buffer coverage, not elapsed time.
    if (this.uploadRanges.length > 256)
      this.uploadRanges = mergeRanges(this.uploadRanges);
    this.changed();
  }
  /** Constant-time removal by swapping the last visible slot into its hole. */
  private setVisible(slot: number, visible: boolean) {
    const at = this.visibleSlots![slot];
    if (visible === Boolean(at)) return;
    if (visible) {
      this.visibleIndices[this.visibleCount] = slot;
      this.visibleSlots![slot] = this.visibleCount + 1;
      this.visibleDirty.add(this.visibleCount >>> 5);
      this.visibleCount++;
    } else {
      const index = at - 1;
      const last = this.visibleIndices[--this.visibleCount];
      this.visibleSlots![slot] = 0;
      if (index < this.visibleCount) {
        this.visibleIndices[index] = last;
        this.visibleSlots![last] = index + 1;
        this.visibleDirty.add(index >>> 5);
      }
    }
  }
  takeVisibleRanges() {
    const ranges: { start: number; count: number }[] = [];
    // Dirty pages avoid sorting tens of thousands of individual index edits.
    for (const page of [...this.visibleDirty].sort((a, b) => a - b)) {
      const start = page * 32;
      const end = Math.min(start + 32, this.visibleCount);
      if (end <= start) continue;
      const last = ranges.at(-1);
      if (last && start <= last.start + last.count + 32)
        last.count = end - last.start;
      else ranges.push({ start, count: end - start });
    }
    this.visibleDirty.clear();
    const uploads = this.visibleUploads;
    this.visibleUploads = [];
    return uploads.length ? mergeRanges([...uploads, ...ranges]) : ranges;
  }
  private selectMap(next: string) {
    if (this.mapKey === next) return;
    if (this.mapKey !== null) this.retired.add(this.mapKey);
    if (this.retired.size > 16)
      this.retired.delete(this.retired.values().next().value!);
    this.empty();
    this.mapKey = next;
  }
  /** The backend's objects snapshot announces a new map even before points arrive. */
  announce(value: unknown) {
    if (!record(value) || value.version !== 1 || value.type !== "objects")
      return false;
    const next = identity(value);
    if (!next || keyFor(next) === this.mapKey) return false;
    this.retired.delete(keyFor(next));
    this.selectMap(keyFor(next));
    this.changed();
    return true;
  }
  ingest(value: unknown): "accepted" | "ignored" | "invalid" {
    const chunk = parsePointChunk(value);
    if (!chunk) return "invalid";
    return this.ingestChunk(chunk);
  }
  /** Internal calibrated captures; the public v1/v2 wire contract is unchanged. */
  ingestCaptured(value: CapturedPoints): "accepted" | "ignored" | "invalid" {
    if (
      !identity({ session_id: value.sessionId, map_epoch: value.mapEpoch }) ||
      !integer(value.frameId) ||
      !Number.isFinite(value.capturedAt) ||
      value.capturedAt < 0 ||
      !(value.positions instanceof Float32Array) ||
      !(value.colors instanceof Float32Array) ||
      !value.positions.length ||
      value.positions.length % 3 ||
      value.positions.length > 65_536 * 3 ||
      value.colors.length !== value.positions.length ||
      (value.covered !== undefined &&
        (!(value.covered instanceof Uint8Array) ||
          value.covered.length !== value.positions.length / 3 ||
          !value.covered.every((v) => v <= 1))) ||
      !value.positions.every((v) => Number.isFinite(v) && Math.abs(v) <= 1e6) ||
      !value.colors.every((v) => Number.isFinite(v) && v >= 0 && v <= 1)
    )
      return "invalid";
    const removed =
      value.retirement &&
      keyFor({ session_id: value.sessionId, map_epoch: value.mapEpoch }) ===
        this.mapKey
        ? this.retireDepth(value.retirement, value.capturedAt)
        : false;
    const result = this.ingestChunk(
      {
        version: 1,
        type: "points",
        chunk_id: value.frameId,
        session_id: value.sessionId,
        map_epoch: value.mapEpoch,
        t_capture: value.capturedAt,
        positions: value.positions,
        colors: value.colors,
      },
      true,
      value.covered,
    );
    if (removed && result !== "accepted") {
      this.changed();
      return "accepted";
    }
    return result;
  }
  /** Retire only disproven samples; bounded work advances fairly across the cache. */
  private retireDepth(observations: DepthObservation[], capturedAt: number) {
    if (
      !this.count ||
      !observations[1] ||
      observations[1].capturedAt <= this.lastRetirement ||
      observations[1].capturedAt !== capturedAt
    )
      return;
    let proof: DepthContradiction;
    try {
      proof = new DepthContradiction(observations);
    } catch {
      return;
    }
    if (proof.mapKey !== this.mapKey) return;
    this.lastRetirement = proof.capturedAt;
    let removed = false;
    const wasAtCapacity = this.retirementCapacity;
    const limit = Math.min(this.count, POINT_RETIREMENT_VISITS);
    const started = performance.now();
    const deadline = started + 12;
    const visit = (slot: number) => {
      if (slot >= this.count) return false;
      const key = this.keys[slot]!;
      const canRemember =
        this.retiredVoxels.has(key) ||
        this.retiredVoxels.size <
          Math.min(this.capacity, MAX_POINT_RETIREMENTS);
      if (
        (canRemember || !this.retirementCapacity) &&
        this.observedAt![slot] < proof.before &&
        proof.point(
          this.positions[slot * 3],
          this.positions[slot * 3 + 1],
          this.positions[slot * 3 + 2],
        )
      ) {
        if (!canRemember) {
          this.retirementCapacity = true;
          return false;
        }
        // Keep exact spatial tombstones until map reset. At capacity, preserve
        // further samples rather than delete without resurrection protection.
        this.retiredVoxels.set(
          key,
          Math.max(this.retiredVoxels.get(key) ?? -1, proof.before),
        );
        this.retiredVoxelCount = this.retiredVoxels.size;
        this.removeSlot(slot);
        removed = true;
        return true;
      }
      return false;
    };
    // Recent insertions get a small bounded head start; historical work keeps
    // its own cursor and the majority of the time budget, so neither can starve.
    let visited = 0;
    for (let i = 0; i < Math.min(this.recentRetirementCount, 256); i++) {
      if (
        visited >= limit ||
        (i % 32 === 0 && performance.now() >= started + 4)
      )
        break;
      const index = (this.recentRetirementCursor - 1 - i + 1024) % 1024;
      visit(this.recentRetirementSlots[index]);
      visited++;
    }
    for (; visited < limit && this.count; visited++) {
      if (visited && visited % 128 === 0 && performance.now() >= deadline)
        break;
      this.retirementCursor %= this.count;
      if (!visit(this.retirementCursor)) this.retirementCursor++;
    }
    return removed || wasAtCapacity !== this.retirementCapacity;
  }
  private removeSlot(slot: number) {
    const last = this.count - 1;
    this.slots.delete(this.keys[slot]!);
    this.setVisible(slot, false);
    if (slot !== last) {
      this.positions.copyWithin(slot * 3, last * 3, last * 3 + 3);
      this.colors.copyWithin(slot * 3, last * 3, last * 3 + 3);
      this.observedAt![slot] = this.observedAt![last];
      this.keys[slot] = this.keys[last];
      this.slots.set(this.keys[slot]!, slot);
      const at = this.visibleSlots![last];
      this.visibleSlots![slot] = at;
      if (at) {
        this.visibleIndices[at - 1] = slot;
        this.visibleDirty.add((at - 1) >>> 5);
      }
      this.dirty.add(slot);
    }
    this.visibleSlots![last] = 0;
    this.keys[last] = undefined;
    this.dirty.delete(last);
    this.count--;
    this.cursor = this.count;
  }
  private ingestChunk(
    chunk: PointChunk,
    measured = false,
    covered?: Uint8Array,
  ): "accepted" | "ignored" {
    const next = keyFor(
      chunk.session_id
        ? { session_id: chunk.session_id, map_epoch: chunk.map_epoch! }
        : null,
    );
    if (this.retired.has(next)) return "ignored";
    // Once a source provides map identity, unidentified chunks cannot enter it.
    if (next === "legacy" && this.mapKey !== null && this.mapKey !== "legacy")
      return "ignored";
    const sameMap = next === this.mapKey;
    if (
      sameMap &&
      (chunk.t_capture !== undefined
        ? measured && covered
          ? chunk.t_capture < this.lastMeasured
          : chunk.t_capture <= (measured ? this.lastMeasured : this.lastCapture)
        : chunk.chunk_id <= this.lastChunk)
    )
      return "ignored";
    this.selectMap(next);
    this.observedAt ??= new Float64Array(this.capacity);
    this.visibleSlots ??= new Uint32Array(this.capacity);
    for (let i = 0; i < chunk.positions.length; i += 3) {
      const x = chunk.positions[i];
      const y = chunk.positions[i + 1];
      const z = chunk.positions[i + 2];
      const voxel = voxelKey(x, y, z, this.voxelSize);
      const retiredAt = this.retiredVoxels.get(voxel);
      if (
        retiredAt !== undefined &&
        (chunk.t_capture === undefined || chunk.t_capture <= retiredAt)
      )
        continue;
      let slot = this.slots.get(voxel);
      const isNew = slot === undefined;
      const displacement =
        slot === undefined
          ? Infinity
          : (this.positions[slot * 3] - x) ** 2 +
            (this.positions[slot * 3 + 1] - y) ** 2 +
            (this.positions[slot * 3 + 2] - z) ** 2;
      // A delayed full capture can add unseen detail without replacing newer
      // observations from the live point stream in an already observed cell.
      if (
        slot !== undefined &&
        chunk.t_capture !== undefined &&
        chunk.t_capture < this.observedAt[slot]
      ) {
        // Late confirmed geometry can still retire a matching cached sample,
        // but cannot move it or hide a newer foreground measurement.
        if (covered?.[i / 3] && displacement <= 0.003 ** 2)
          this.setVisible(slot, false);
        continue;
      }
      if (slot === undefined) {
        slot = this.cursor;
        const previous = this.keys[slot];
        if (previous !== undefined) {
          this.slots.delete(previous);
          this.evicted++;
        }
        this.keys[slot] = voxel;
        this.slots.set(voxel, slot);
        this.cursor = (this.cursor + 1) % this.capacity;
        this.count = Math.min(this.capacity, this.count + 1);
      }
      if (isNew || displacement > 0.003 ** 2) {
        this.recentRetirementSlots[this.recentRetirementCursor] = slot;
        this.recentRetirementCursor = (this.recentRetirementCursor + 1) % 1024;
        this.recentRetirementCount = Math.min(
          1024,
          this.recentRetirementCount + 1,
        );
      }
      const index = slot * 3;
      const hidden = covered
        ? covered[i / 3] === 1
        : !isNew && !this.visibleSlots[slot] && displacement <= 0.003 ** 2;
      this.setVisible(slot, !hidden);
      let updated = isNew;
      for (let channel = 0; channel < 3; channel++) {
        const p = Math.fround(chunk.positions[i + channel]);
        const c = measured
          ? chunk.colors[i + channel]
          : chunk.version === 2
            ? rgb8Linear[chunk.colors[i + channel]]
            : Math.fround(linearColor(chunk.colors[i + channel]));
        if (
          this.positions[index + channel] !== p ||
          this.colors[index + channel] !== c
        )
          updated = true;
        this.positions[index + channel] = p;
        this.colors[index + channel] = c;
      }
      if (updated) this.dirty.add(slot);
      this.observedAt[slot] = chunk.t_capture ?? NaN;
    }
    if (measured) this.lastMeasured = chunk.t_capture!;
    else {
      this.lastChunk = chunk.chunk_id;
      if (chunk.t_capture !== undefined) this.lastCapture = chunk.t_capture;
    }
    this.changed();
    return "accepted";
  }
  bounds(): CloudBounds | null {
    if (!this.count) return null;
    const min = [Infinity, Infinity, Infinity],
      max = [-Infinity, -Infinity, -Infinity];
    for (let i = 0; i < this.count * 3; i++) {
      const axis = i % 3;
      min[axis] = Math.min(min[axis], this.positions[i]);
      max[axis] = Math.max(max[axis], this.positions[i]);
    }
    return {
      center: [
        (min[0] + max[0]) / 2,
        (min[1] + max[1]) / 2,
        (min[2] + max[2]) / 2,
      ],
      radius: Math.max(
        0.1,
        Math.hypot(max[0] - min[0], max[1] - min[1], max[2] - min[2]) / 2,
      ),
    };
  }
}
