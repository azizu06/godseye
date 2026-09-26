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
  if (patch.positions.length % 3 || patch.indices.length % 3)
    throw Error("Invalid surface geometry");
  for (const value of patch.positions)
    if (!Number.isFinite(value)) throw Error("Invalid surface position");
  for (const index of patch.indices)
    if (index >= patch.positions.length / 3)
      throw Error("Invalid surface index");
  const colorAt = (u: number, v: number): number[] => {
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
    return [0, 1, 2].map(
      (c) =>
        linear[image.data[(y0 * image.width + x0) * 4 + c]] *
          (1 - dx) *
          (1 - dy) +
        linear[image.data[(y0 * image.width + x1) * 4 + c]] * dx * (1 - dy) +
        linear[image.data[(y1 * image.width + x0) * 4 + c]] * (1 - dx) * dy +
        linear[image.data[(y1 * image.width + x1) * 4 + c]] * dx * dy,
    );
  };
  const positions = [...patch.positions],
    uvs = [...patch.uvs],
    colors: number[] = [];
  for (let i = 0; i < positions.length / 3; i++)
    colors.push(...colorAt(uvs[i * 2], uvs[i * 2 + 1]));
  // Refine appearance within already-supported geometry, never across a missing
  // face. A bounded breadth-first pass gives all observed regions a first turn.
  // These probes detect detail; they do not certify a per-pixel error bound.
  const maxVertices = Math.max(positions.length / 3, 6_144);
  const queue: { ids: number[]; depth: number }[] = [];
  for (let i = 0; i < patch.indices.length; i += 3)
    queue.push({ ids: [...patch.indices.subarray(i, i + 3)], depth: 0 });
  const indices: number[] = [],
    midpoints = new Map<string, number>();
  const key = (a: number, b: number) => (a < b ? `${a}:${b}` : `${b}:${a}`);
  const midpoint = (a: number, b: number) => {
    const edge = key(a, b),
      known = midpoints.get(edge);
    if (known !== undefined) return known;
    const id = positions.length / 3;
    for (let c = 0; c < 3; c++)
      positions.push((positions[a * 3 + c] + positions[b * 3 + c]) / 2);
    const u = (uvs[a * 2] + uvs[b * 2]) / 2,
      v = (uvs[a * 2 + 1] + uvs[b * 2 + 1]) / 2;
    uvs.push(u, v);
    colors.push(...colorAt(u, v));
    midpoints.set(edge, id);
    return id;
  };
  for (let cursor = 0; cursor < queue.length; cursor++) {
    const { ids, depth } = queue[cursor],
      [a, b, c] = ids;
    const span =
      Math.max(...ids.map((i) => uvs[i * 2])) -
      Math.min(...ids.map((i) => uvs[i * 2]));
    const height =
      Math.max(...ids.map((i) => uvs[i * 2 + 1])) -
      Math.min(...ids.map((i) => uvs[i * 2 + 1]));
    const edges = [
      [a, b],
      [b, c],
      [c, a],
    ];
    const needed = edges.filter(([i, j]) => !midpoints.has(key(i, j))).length;
    let detail = false;
    if (
      depth < 4 &&
      Math.max(span * image.width, height * image.height) > 2 &&
      positions.length / 3 + needed <= maxVertices
    ) {
      // Quarter-edge/interior probes catch marks absent at all coarse corners.
      for (let p = 0; p <= 4 && !detail; p++)
        for (let q = 0; q <= 4 - p; q++) {
          if ((p === 0 && q === 0) || p === 4 || q === 4) continue;
          const weights = [p / 4, q / 4, 1 - (p + q) / 4];
          const u = ids.reduce((n, id, j) => n + weights[j] * uvs[id * 2], 0);
          const v = ids.reduce(
            (n, id, j) => n + weights[j] * uvs[id * 2 + 1],
            0,
          );
          const actual = colorAt(u, v);
          detail = actual.some(
            (value, channel) =>
              Math.abs(
                value -
                  ids.reduce(
                    (n, id, j) => n + weights[j] * colors[id * 3 + channel],
                    0,
                  ),
              ) > 0.06,
          );
          if (detail) break;
        }
    }
    if (!detail) {
      indices.push(a, b, c);
      continue;
    }
    const ab = midpoint(a, b),
      bc = midpoint(b, c),
      ca = midpoint(c, a);
    for (const child of [
      [a, ab, ca],
      [ab, b, bc],
      [ca, bc, c],
      [ab, bc, ca],
    ])
      queue.push({ ids: child, depth: depth + 1 });
  }
  return {
    id: patch.id,
    positions: new Float32Array(positions),
    indices: new Uint32Array(indices),
    colors: new Float32Array(colors),
  };
}
/** Export referenced vertices only; buffers may also contain unused vertices. */
export function serializeSurface(
  patch: SurfacePatch | null,
  cellM: number | null,
) {
  if (!patch) return null;
  const ids = new Map<number, number>(),
    positions: number[] = [],
    colors: number[] = [],
    indices: number[] = [];
  for (const index of patch.indices) {
    let id = ids.get(index);
    if (id === undefined) {
      id = ids.size;
      ids.set(index, id);
      positions.push(...patch.positions.subarray(index * 3, index * 3 + 3));
      if (patch.colors)
        colors.push(...patch.colors.subarray(index * 3, index * 3 + 3));
    }
    indices.push(id);
  }
  return {
    coordinates: "ARKit world meters, +Y up",
    color_space: "linear-sRGB",
    cell_m: cellM,
    positions,
    colors,
    indices,
  };
}
