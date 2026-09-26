/** Observed triangle geometry in ARKit world meters, never an inferred room shell. */
export interface SurfacePatch {
  id: string;
  positions: Float32Array;
  indices: Uint32Array;
  colors?: Float32Array;
  uvs?: Float32Array;
  jpeg?: Uint8Array;
  image?: HTMLCanvasElement | ImageBitmap;
  update?: {
    revision: number;
    vertexStart: number;
    indexStart: number;
    positions: Float32Array;
    colors: Float32Array;
    indices: Uint32Array;
  };
}
export interface CapturedSurface extends SurfacePatch {
  sessionId: string;
  mapEpoch: number;
  frameId: number;
  capturedAt: number;
  depthWidth?: number;
  depthHeight?: number;
  projection?: {
    transform: number[];
    intrinsics: number[];
    imageWidth: number;
    imageHeight: number;
  };
  cameraPosition: [number, number, number];
  cameraForward: [number, number, number];
}
export type SurfaceStatus =
  "waiting" | "receiving" | "unavailable" | "error" | "paused" | "capacity";
