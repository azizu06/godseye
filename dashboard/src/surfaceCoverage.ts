import type { CapturedSurface } from "./surfaceTypes";

/** Remove only measured samples inside retained faces and within 5 mm of them.
 * Rasterization uses the capture's depth grid, so gaps and foreground samples
 * cannot inherit coverage just because they share a coarse world-space cell.
 */
function visitSamples(
  surface: CapturedSurface,
  retained: Uint32Array,
  toleranceMeters: number,
  accept: (sample: number, triangle: number) => void,
) {
  const { positions: p, uvs, depthWidth: width, depthHeight: height } = surface;
  if (!width || !height || !uvs || !retained.length) return;
  const count = p.length / 3;
  const lookup = new Int32Array(width * height).fill(-1);
  const pixels = new Float32Array(count * 2);
  for (let i = 0; i < count; i++) {
    const x = Math.round(uvs[i * 2] * width - 0.5);
    const y = Math.round((1 - uvs[i * 2 + 1]) * height - 0.5);
    pixels[i * 2] = x;
    pixels[i * 2 + 1] = y;
    if (x >= 0 && x < width && y >= 0 && y < height) lookup[y * width + x] = i;
  }
  for (let i = 0; i < retained.length; i += 3) {
    const a = retained[i],
      b = retained[i + 1],
      c = retained[i + 2];
    const ax = pixels[a * 2],
      ay = pixels[a * 2 + 1];
    const bx = pixels[b * 2],
      by = pixels[b * 2 + 1];
    const cx = pixels[c * 2],
      cy = pixels[c * 2 + 1];
    const denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy);
    if (!denominator) continue;
    const ux = p[b * 3] - p[a * 3],
      uy = p[b * 3 + 1] - p[a * 3 + 1],
      uz = p[b * 3 + 2] - p[a * 3 + 2];
    const vx = p[c * 3] - p[a * 3],
      vy = p[c * 3 + 1] - p[a * 3 + 1],
      vz = p[c * 3 + 2] - p[a * 3 + 2];
    const nx = uy * vz - uz * vy,
      ny = uz * vx - ux * vz,
      nz = ux * vy - uy * vx;
    const tolerance = toleranceMeters * Math.hypot(nx, ny, nz);
    if (!tolerance) continue;
    for (
      let y = Math.max(0, Math.min(ay, by, cy));
      y <= Math.min(height - 1, Math.max(ay, by, cy));
      y++
    ) {
      for (
        let x = Math.max(0, Math.min(ax, bx, cx));
        x <= Math.min(width - 1, Math.max(ax, bx, cx));
        x++
      ) {
        const sample = lookup[y * width + x];
        if (sample < 0) continue;
        const wa = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / denominator;
        const wb = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / denominator;
        if (wa < -1e-6 || wb < -1e-6 || wa + wb > 1 + 1e-6) continue;
        const distance =
          (p[sample * 3] - p[a * 3]) * nx +
          (p[sample * 3 + 1] - p[a * 3 + 1]) * ny +
          (p[sample * 3 + 2] - p[a * 3 + 2]) * nz;
        if (Math.abs(distance) <= tolerance) accept(sample, i / 3);
      }
    }
  }
}

/** Native sample counts provide spatial evidence even before temporal revisits. */
export function surfaceSupport(surface: CapturedSurface): Uint8Array {
  const counts = new Uint8Array(surface.indices.length / 3);
  visitSamples(surface, surface.indices, 0.008, (_sample, triangle) => {
    if (counts[triangle] < 255) counts[triangle]++;
  });
  return counts;
}

export function surfaceCoverage(
  surface: CapturedSurface,
  retained: Uint32Array,
): Uint8Array {
  const covered = new Uint8Array(surface.positions.length / 3);
  visitSamples(surface, retained, 0.005, (sample) => {
    covered[sample] = 1;
  });
  return covered;
}
