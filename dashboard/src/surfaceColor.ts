import type { SurfacePatch } from "./surfaceTypes";
export interface ColorPixels {
  width: number;
  height: number;
  data: Uint8ClampedArray;
}
const linear = Float32Array.from({ length: 256 }, (_, value) => {
  const c = value / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
});
/** Bake the same-frame, top-left-origin image through Three's bottom-left UVs. */
export function bakeSurfaceColors(
  patch: SurfacePatch,
  image: ColorPixels,
): SurfacePatch {
  if (
    !patch.uvs ||
    patch.uvs.length !== (patch.positions.length / 3) * 2 ||
    image.width < 1 ||
    image.height < 1 ||
    image.data.length !== image.width * image.height * 4
  )
    throw Error("Invalid surface color input");
  const colors = new Float32Array(patch.positions.length);
  for (let i = 0; i < patch.positions.length / 3; i++) {
    const u = patch.uvs[i * 2],
      v = patch.uvs[i * 2 + 1];
    if (!Number.isFinite(u) || !Number.isFinite(v))
      throw Error("Invalid surface UV");
    const x = Math.max(0, Math.min(image.width - 1, u * image.width - 0.5));
    const y = Math.max(
      0,
      Math.min(image.height - 1, (1 - v) * image.height - 0.5),
    );
    const x0 = Math.floor(x),
      y0 = Math.floor(y),
      x1 = Math.min(x0 + 1, image.width - 1),
      y1 = Math.min(y0 + 1, image.height - 1),
      dx = x - x0,
      dy = y - y0;
    for (let c = 0; c < 3; c++)
      colors[i * 3 + c] =
        linear[image.data[(y0 * image.width + x0) * 4 + c]] *
          (1 - dx) *
          (1 - dy) +
        linear[image.data[(y0 * image.width + x1) * 4 + c]] * dx * (1 - dy) +
        linear[image.data[(y1 * image.width + x0) * 4 + c]] * (1 - dx) * dy +
        linear[image.data[(y1 * image.width + x1) * 4 + c]] * dx * dy;
  }
  return {
    id: patch.id,
    positions: patch.positions,
    indices: patch.indices,
    colors,
  };
}
