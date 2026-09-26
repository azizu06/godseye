import { ShapeUtils, Vector2 } from "three";
import type { SurfacePatch } from "./surfaceTypes";

type Vec = [number, number, number];
type Face = { indices: Vec; normal: Vec; area: number; color: Vec };
const MIN_FACES = 64,
  NORMAL_AGREEMENT = Math.cos((12 * Math.PI) / 180),
  MAX_RESIDUAL_M = 0.008,
  MAX_COLOR_ERROR = 0.04;
const sub = (a: Vec, b: Vec) => a.map((v, i) => v - b[i]) as Vec;
const dot = (a: Vec, b: Vec) => a.reduce((sum, v, i) => sum + v * b[i], 0);
const cross = (a: Vec, b: Vec): Vec => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
];
const unit = (a: Vec): Vec => {
  const length = Math.hypot(...a);
  return a.map((v) => v / (length || 1)) as Vec;
};
const edgeKey = (a: number, b: number) => (a < b ? `${a},${b}` : `${b},${a}`);
const signedArea = (loop: number[], uv: Map<number, Vector2>) =>
  loop.reduce((sum, index, i) => {
    const a = uv.get(index)!,
      b = uv.get(loop[(i + 1) % loop.length])!;
    return sum + a.x * b.y - a.y * b.x;
  }, 0) / 2;
function contains(loop: number[], point: Vector2, uv: Map<number, Vector2>) {
  let inside = false;
  for (let i = 0, j = loop.length - 1; i < loop.length; j = i++) {
    const a = uv.get(loop[i])!,
      b = uv.get(loop[j])!;
    if (
      a.y > point.y !== b.y > point.y &&
      point.x < ((b.x - a.x) * (point.y - a.y)) / (b.y - a.y) + a.x
    )
      inside = !inside;
  }
  return inside;
}
function boundaryLoops(
  region: number[],
  faces: Face[],
  uv: Map<number, Vector2>,
): number[][] | null {
  const edges = new Map<string, { a: number; b: number; count: number }>();
  for (const index of region) {
    const face = faces[index];
    for (let e = 0; e < 3; e++) {
      const a = face.indices[e],
        b = face.indices[(e + 1) % 3],
        key = edgeKey(a, b),
        old = edges.get(key);
      if (old) {
        if (++old.count > 2) return null;
      } else edges.set(key, { a, b, count: 1 });
    }
  }
  const neighbors = new Map<number, number[]>();
  for (const { a, b, count } of edges.values())
    if (count === 1) {
      neighbors.set(a, [...(neighbors.get(a) ?? []), b]);
      neighbors.set(b, [...(neighbors.get(b) ?? []), a]);
    }
  if (
    neighbors.size > 2048 ||
    [...neighbors.values()].some((n) => n.length !== 2)
  )
    return null;
  const visited = new Set<number>(),
    loops: number[][] = [];
  for (const first of neighbors.keys()) {
    if (visited.has(first)) continue;
    const loop: number[] = [];
    let previous = -1,
      current = first;
    do {
      if (visited.has(current)) return null;
      visited.add(current);
      loop.push(current);
      const next = neighbors.get(current)!.find((index) => index !== previous)!;
      previous = current;
      current = next;
    } while (current !== first);
    if (loop.length < 3) return null;
    // Drop only redundant straight-edge samples, not noisy/concave boundaries.
    let changed = true;
    while (changed && loop.length > 3) {
      changed = false;
      for (let i = 0; i < loop.length && loop.length > 3; i++) {
        const a = uv.get(loop[(i + loop.length - 1) % loop.length])!,
          b = uv.get(loop[i])!,
          c = uv.get(loop[(i + 1) % loop.length])!;
        const length = a.distanceTo(c),
          distance =
            Math.abs((c.x - a.x) * (b.y - a.y) - (c.y - a.y) * (b.x - a.x)) /
            (length || 1);
        if (
          length > 0 &&
          distance <= 1e-6 &&
          (b.x - a.x) * (b.x - c.x) + (b.y - a.y) * (b.y - c.y) <= 1e-12
        ) {
          loop.splice(i--, 1);
          changed = true;
        }
      }
    }
    loops.push(loop);
    if (loops.length > 32) return null;
  }
  return loops.length ? loops : null;
}

type RegionResult = { patch: SurfacePatch } | "color" | null;
function simplifyRegion(
  region: number[],
  faces: Face[],
  patch: SurfacePatch,
): RegionResult {
  if (region.length < MIN_FACES) return null;
  const ids = [...new Set(region.flatMap((index) => faces[index].indices))];
  if (ids.length < 32) return null;
  const position = (index: number) =>
    [...patch.positions.slice(index * 3, index * 3 + 3)] as Vec;
  const reference = faces[region[0]].normal,
    normal: Vec = [0, 0, 0],
    origin: Vec = [0, 0, 0];
  for (const index of region) {
    const face = faces[index],
      sign = dot(face.normal, reference) < 0 ? -1 : 1;
    for (let a = 0; a < 3; a++) normal[a] += face.normal[a] * face.area * sign;
  }
  const n = unit(normal);
  if (Math.hypot(...n) < 0.9) return null;
  for (const id of ids) {
    const p = position(id);
    for (let a = 0; a < 3; a++) origin[a] += p[a] / ids.length;
  }
  if (
    region.some(
      (index) => Math.abs(dot(faces[index].normal, n)) < NORMAL_AGREEMENT,
    )
  )
    return null;
  if (
    ids.some(
      (id) => Math.abs(dot(sub(position(id), origin), n)) > MAX_RESIDUAL_M,
    )
  )
    return null;
  const referenceAxis: Vec = Math.abs(n[0]) < 0.8 ? [1, 0, 0] : [0, 1, 0],
    u = unit(cross(n, referenceAxis)),
    v = cross(n, u);
  const uv = new Map<number, Vector2>();
  let suu = 0,
    suv = 0,
    svv = 0;
  const mean: Vec = [0, 0, 0],
    uc: Vec = [0, 0, 0],
    vc: Vec = [0, 0, 0];
  for (const id of ids) {
    const delta = sub(position(id), origin),
      a = dot(delta, u),
      b = dot(delta, v);
    uv.set(id, new Vector2(a, b));
    suu += a * a;
    suv += a * b;
    svv += b * b;
    for (let channel = 0; channel < 3; channel++) {
      const color = patch.colors![id * 3 + channel];
      mean[channel] += color / ids.length;
      uc[channel] += a * color;
      vc[channel] += b * color;
    }
  }
  const determinant = suu * svv - suv * suv;
  if (!Number.isFinite(determinant) || determinant < 1e-15) return null;
  const slopeU = uc.map(
      (value, c) => (value * svv - vc[c] * suv) / determinant,
    ),
    slopeV = vc.map((value, c) => (value * suu - uc[c] * suv) / determinant);
  const colorAt = (point: Vector2) =>
    mean.map(
      (color, c) => color + slopeU[c] * point.x + slopeV[c] * point.y,
    ) as Vec;
  if (
    ids.some((id) =>
      colorAt(uv.get(id)!).some(
        (color, c) =>
          color < -1e-7 ||
          color > 1 + 1e-7 ||
          Math.abs(color - patch.colors![id * 3 + c]) >
            MAX_COLOR_ERROR / 2 - 1e-6,
      ),
    )
  )
    return "color";
  // Keep original boundary colors so adjacent regions agree at coincident
  // vertices. Each sample and corner is within half the color-error budget of
  // one affine field, bounding their interpolated difference by the full budget.
  const loops = boundaryLoops(region, faces, uv);
  if (!loops) return null;
  loops.sort(
    (a, b) => Math.abs(signedArea(b, uv)) - Math.abs(signedArea(a, uv)),
  );
  const [outer, ...holes] = loops;
  if (holes.some((hole) => !contains(outer, uv.get(hole[0])!, uv))) return null;
  const footprint =
    Math.abs(signedArea(outer, uv)) -
    holes.reduce((sum, hole) => sum + Math.abs(signedArea(hole, uv)), 0);
  const observed = region.reduce(
    (sum, index) => sum + Math.abs(signedArea([...faces[index].indices], uv)),
    0,
  );
  if (
    footprint <= 0 ||
    Math.abs(footprint - observed) > Math.max(1e-9, observed * 1e-5)
  )
    return null;
  const retained = loops.flat();
  if (retained.length > ids.length * 0.7) return null;
  if (
    new Set(
      retained.map((id) => {
        const p = uv.get(id)!;
        return `${p.x},${p.y}`;
      }),
    ).size !== retained.length
  )
    return null;
  const triangles = ShapeUtils.triangulateShape(
    outer.map((id) => uv.get(id)!.clone()),
    holes.map((hole) => hole.map((id) => uv.get(id)!.clone())),
  );
  if (!triangles.length || triangles.length >= region.length) return null;
  const triangulated = triangles.reduce(
    (sum, triangle) =>
      sum +
      Math.abs(
        signedArea(
          triangle.map((index) => retained[index]),
          uv,
        ),
      ),
    0,
  );
  if (Math.abs(triangulated - footprint) > Math.max(1e-9, footprint * 1e-5))
    return null;
  const positions: number[] = [],
    colors: number[] = [];
  for (const id of retained) {
    const point = uv.get(id)!;
    positions.push(
      ...origin.map(
        (value, axis) => value + u[axis] * point.x + v[axis] * point.y,
      ),
    );
    colors.push(...patch.colors!.slice(id * 3, id * 3 + 3));
  }
  if (positions.some((value) => !Number.isFinite(Math.fround(value))))
    return null;
  return {
    patch: {
      id: patch.id,
      positions: new Float32Array(positions),
      colors: new Float32Array(colors),
      indices: new Uint32Array(triangles.flat()),
    },
  };
}

/** Validated colored triangles only. No complete plane, source image, or hidden mesh is cached. */
export function simplifyPlanarPatch(patch: SurfacePatch): SurfacePatch {
  if (patch.indices.length < MIN_FACES * 3 || !patch.colors) return patch;
  const position = (index: number) =>
    [...patch.positions.slice(index * 3, index * 3 + 3)] as Vec;
  const faces: Face[] = [],
    edges = new Map<string, number[]>();
  for (let offset = 0; offset < patch.indices.length; offset += 3) {
    const indices = [...patch.indices.slice(offset, offset + 3)] as Vec,
      [a, b, c] = indices.map(position),
      normal = cross(sub(b, a), sub(c, a)),
      area = Math.hypot(...normal);
    const face = {
        indices,
        normal: unit(normal),
        area,
        color: [0, 1, 2].map(
          (channel) =>
            indices.reduce(
              (sum, index) => sum + patch.colors![index * 3 + channel],
              0,
            ) / 3,
        ) as Vec,
      },
      index = faces.length;
    faces.push(face);
    for (let e = 0; e < 3; e++) {
      const key = edgeKey(indices[e], indices[(e + 1) % 3]),
        list = edges.get(key);
      if (list) list.push(index);
      else edges.set(key, [index]);
    }
  }
  const neighbors = faces.map(() => [] as number[]);
  for (const list of edges.values())
    if (list.length === 2) {
      neighbors[list[0]].push(list[1]);
      neighbors[list[1]].push(list[0]);
    }
  const connected = (candidates: number[], colorBins = false) => {
    const allowed = new Set(candidates),
      seen = new Set<number>(),
      groups: number[][] = [];
    const colorKey = (index: number) =>
      faces[index].color.map((c) => Math.floor(c / 0.1)).join(",");
    for (const seed of candidates) {
      if (seen.has(seed)) continue;
      const queue = [seed],
        group: number[] = [],
        key = colorKey(seed);
      seen.add(seed);
      while (queue.length) {
        const index = queue.pop()!;
        group.push(index);
        for (const next of neighbors[index])
          if (
            allowed.has(next) &&
            !seen.has(next) &&
            Math.abs(dot(faces[seed].normal, faces[next].normal)) >=
              NORMAL_AGREEMENT &&
            (!colorBins || colorKey(next) === key)
          ) {
            seen.add(next);
            queue.push(next);
          }
      }
      groups.push(group);
    }
    return groups;
  };
  const replacement: SurfacePatch[] = [],
    original: number[] = [];
  for (const region of connected(faces.map((_, i) => i))) {
    const result = simplifyRegion(region, faces, patch);
    if (result && result !== "color") replacement.push(result.patch);
    else if (result === "color")
      for (const part of connected(region, true)) {
        const smaller = simplifyRegion(part, faces, patch);
        if (smaller && smaller !== "color") replacement.push(smaller.patch);
        else original.push(...part);
      }
    else original.push(...region);
  }
  if (!replacement.length) return patch;
  const positions: number[] = [],
    colors: number[] = [],
    indices: number[] = [],
    used = new Map<number, number>();
  for (const face of original)
    for (const index of faces[face].indices) {
      let mapped = used.get(index);
      if (mapped === undefined) {
        mapped = positions.length / 3;
        used.set(index, mapped);
        positions.push(...position(index));
        colors.push(...patch.colors.slice(index * 3, index * 3 + 3));
      }
      indices.push(mapped);
    }
  for (const region of replacement) {
    const base = positions.length / 3;
    for (const value of region.positions) positions.push(value);
    for (const value of region.colors!) colors.push(value);
    for (const index of region.indices) indices.push(base + index);
  }
  return {
    id: patch.id,
    positions: new Float32Array(positions),
    colors: new Float32Array(colors),
    indices: new Uint32Array(indices),
  };
}
