export const MAX_POINTS = 250_000;
export const VOXEL_SIZE = 0.01;
export const MAX_CHUNK_POINTS = 2500;

type Identity = { session_id: string; map_epoch: number };
export type PointChunk = {
  version: 1;
  type: "points";
  chunk_id: number;
  positions: number[];
  colors: number[];
  session_id?: string;
  map_epoch?: number;
  t_capture?: number;
};
export type CloudBounds = { center: [number, number, number]; radius: number };

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const integer = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

function identity(value: Record<string, unknown>): Identity | null {
  return typeof value.session_id === "string" &&
    value.session_id.length > 0 &&
    value.session_id.length <= 128 &&
    integer(value.map_epoch) &&
    value.map_epoch > 0
    ? { session_id: value.session_id, map_epoch: value.map_epoch }
    : null;
}
const keyFor = (value: Identity | null) =>
  value ? JSON.stringify([value.session_id, value.map_epoch]) : "legacy";

export function parsePointChunk(value: unknown): PointChunk | null {
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

/** JPEG RGB is sRGB; Three.js vertex colors use linear RGB. */
export function linearColor(value: number) {
  return value <= 0.04045
    ? value / 12.92
    : Math.pow((value + 0.055) / 1.055, 2.4);
}

/** Bounded spatial cache. Retain measured positions, never snap them to voxel centers. */
export class PointCloudStore {
  readonly positions: Float32Array;
  readonly colors: Float32Array;
  count = 0;
  evicted = 0;
  private cursor = 0;
  private revision = 0;
  private mapKey: string | null = null;
  private retired = new Set<string>();
  private lastChunk = -1;
  private lastCapture = -1;
  private slots = new Map<string, number>();
  private keys: (string | undefined)[];
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
    this.keys = new Array(capacity);
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
    this.cursor = 0;
    this.evicted = 0;
    this.lastChunk = -1;
    this.lastCapture = -1;
    this.slots.clear();
    this.keys.fill(undefined);
  }
  clear() {
    this.empty();
    this.mapKey = null;
    this.retired.clear();
    this.changed();
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
        ? chunk.t_capture <= this.lastCapture
        : chunk.chunk_id <= this.lastChunk)
    )
      return "ignored";
    this.selectMap(next);
    for (let i = 0; i < chunk.positions.length; i += 3) {
      const x = chunk.positions[i];
      const y = chunk.positions[i + 1];
      const z = chunk.positions[i + 2];
      const voxel = `${Math.floor(x / this.voxelSize)},${Math.floor(y / this.voxelSize)},${Math.floor(z / this.voxelSize)}`;
      let slot = this.slots.get(voxel);
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
      this.positions[slot * 3] = x;
      this.positions[slot * 3 + 1] = y;
      this.positions[slot * 3 + 2] = z;
      for (let channel = 0; channel < 3; channel++)
        this.colors[slot * 3 + channel] = linearColor(
          chunk.colors[i + channel],
        );
    }
    this.lastChunk = chunk.chunk_id;
    if (chunk.t_capture !== undefined) this.lastCapture = chunk.t_capture;
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
