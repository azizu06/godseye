import type { CapturedSurface, SurfacePatch } from "./surfaceTypes";

/** Test measured samples against the actual compressed patch accepted by the map.
 * Projection narrows candidates; world-space barycentrics and a 5 mm plane test
 * preserve openings and foreground. No bounding rectangles imply coverage.
 */
export function retainedCoverage(
  surface: CapturedSurface,
  retained: SurfacePatch,
): Uint8Array {
  const covered = new Uint8Array(surface.positions.length / 3);
  const {
    projection,
    depthWidth: width,
    depthHeight: height,
    uvs,
    positions: samples,
  } = surface;
  if (!projection || !width || !height || !uvs) return covered;
  const {
    transform: t,
    intrinsics: k,
    imageWidth: iw,
    imageHeight: ih,
  } = projection;
  const lookup = new Int32Array(width * height).fill(-1);
  for (let i = 0; i < covered.length; i++) {
    const x = Math.round(uvs[i * 2] * width - 0.5),
      y = Math.round((1 - uvs[i * 2 + 1]) * height - 0.5);
    if (x >= 0 && x < width && y >= 0 && y < height) lookup[y * width + x] = i;
  }
  const p = retained.positions;
  const pixels = new Float32Array((p.length / 3) * 2);
  for (let i = 0; i < p.length / 3; i++) {
    const dx = p[i * 3] - t[12],
      dy = p[i * 3 + 1] - t[13],
      dz = p[i * 3 + 2] - t[14];
    const x = dx * t[0] + dy * t[1] + dz * t[2],
      y = dx * t[4] + dy * t[5] + dz * t[6],
      z = dx * t[8] + dy * t[9] + dz * t[10];
    pixels[i * 2] =
      z < -0.01 ? (((k[0] * x) / -z + k[6]) * width) / iw - 0.5 : NaN;
    pixels[i * 2 + 1] =
      z < -0.01 ? (((-k[4] * y) / -z + k[7]) * height) / ih - 0.5 : NaN;
  }
  // A malformed/near-camera projection must not monopolize the worker.
  let visits = 0;
  for (let i = 0; i < retained.indices.length; i += 3) {
    const [a, b, c] = retained.indices.subarray(i, i + 3);
    const xs = [pixels[a * 2], pixels[b * 2], pixels[c * 2]],
      ys = [pixels[a * 2 + 1], pixels[b * 2 + 1], pixels[c * 2 + 1]];
    if (![...xs, ...ys].every(Number.isFinite)) continue;
    const ux = p[b * 3] - p[a * 3],
      uy = p[b * 3 + 1] - p[a * 3 + 1],
      uz = p[b * 3 + 2] - p[a * 3 + 2];
    const vx = p[c * 3] - p[a * 3],
      vy = p[c * 3 + 1] - p[a * 3 + 1],
      vz = p[c * 3 + 2] - p[a * 3 + 2];
    const nx = uy * vz - uz * vy,
      ny = uz * vx - ux * vz,
      nz = ux * vy - uy * vx;
    const uu = ux * ux + uy * uy + uz * uz,
      uv = ux * vx + uy * vy + uz * vz,
      vv = vx * vx + vy * vy + vz * vz;
    const determinant = uu * vv - uv * uv,
      tolerance = 0.005 * Math.hypot(nx, ny, nz);
    if (determinant <= 0 || !tolerance) continue;
    for (
      let y = Math.max(0, Math.floor(Math.min(...ys)));
      y <= Math.min(height - 1, Math.ceil(Math.max(...ys)));
      y++
    )
      for (
        let x = Math.max(0, Math.floor(Math.min(...xs)));
        x <= Math.min(width - 1, Math.ceil(Math.max(...xs)));
        x++
      ) {
        if (++visits > 2_000_000) return covered; // Partial culling remains conservative.
        const sample = lookup[y * width + x];
        if (sample < 0 || covered[sample]) continue;
        const dx = samples[sample * 3] - p[a * 3],
          dy = samples[sample * 3 + 1] - p[a * 3 + 1],
          dz = samples[sample * 3 + 2] - p[a * 3 + 2];
        if (Math.abs(dx * nx + dy * ny + dz * nz) > tolerance) continue;
        const du = dx * ux + dy * uy + dz * uz,
          dv = dx * vx + dy * vy + dz * vz;
        const bWeight = (vv * du - uv * dv) / determinant,
          cWeight = (uu * dv - uv * du) / determinant;
        if (
          bWeight >= -1e-6 &&
          cWeight >= -1e-6 &&
          bWeight + cWeight <= 1 + 1e-6
        )
          covered[sample] = 1;
      }
  }
  return covered;
}
