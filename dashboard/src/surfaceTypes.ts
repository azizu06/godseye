/** Observed triangle geometry in ARKit world meters, never an inferred room shell. */
export interface SurfacePatch {
  id: string;
  positions: Float32Array;
  indices: Uint32Array;
  colors?: Float32Array;
  uvs?: Float32Array;
  jpeg?: Uint8Array;
  image?: HTMLCanvasElement;
}
export interface CapturedSurface extends SurfacePatch {
  sessionId: string;
  mapEpoch: number;
  frameId: number;
  capturedAt: number;
  cameraPosition: [number, number, number];
  cameraForward: [number, number, number];
}
export type SurfaceStatus =
  "waiting" | "receiving" | "unavailable" | "error" | "paused" | "capacity";
