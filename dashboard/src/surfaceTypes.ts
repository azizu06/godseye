/** Observed triangle geometry in ARKit world meters, never an inferred room shell. */
import type { CloudBounds } from "./pointCloud";

/** Ordered worker delta: packed vertex ranges and newly appended topology only. */
export interface SurfaceTileUpdate {
  id: string;
  revision: number;
  vertexCount: number;
  indexCount: number;
  spans: Uint32Array; // starting component/count pairs into the retained tile
  positions: Float32Array;
  colors: Float32Array;
  indexStart: number;
  indices: Uint32Array;
  bounds: CloudBounds;
}

export interface SurfacePatch {
  id: string;
  positions: Float32Array;
  indices: Uint32Array;
  colors?: Float32Array;
  uvs?: Float32Array;
  jpeg?: Uint8Array;
  image?: ImageBitmap;
}
export interface CapturedSurface extends SurfacePatch {
  sessionId: string;
  mapEpoch: number;
  frameId: number;
  capturedAt: number;
  cameraPosition: [number, number, number];
  cameraForward: [number, number, number];
}
