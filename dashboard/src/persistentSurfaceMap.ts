import type { DepthContradiction } from "./depthRetirement";
import type { SurfacePatch } from "./surfaceTypes";
import type { SurfaceDelta } from "./surfaceBuffer";
import { simplifyPlanarPatch } from "./planarSurface";

type Triple = [number, number, number];
type Vertex = { position: Triple; color: Triple };
type Mesh = { vertices: Vertex[]; triangles: Triple[]; groups?: number[] };
export interface PersistentSurfaceMapOptions {
  maxTriangles?: number;
  maxVertices?: number;
  initialCellM?: number;
}
const triangleKey = (triangle: Triple) =>
  [...triangle].sort((a, b) => a - b).join(",");
const positionKey = (position: Triple) => position.join(",");
const cellKey = (position: Triple, cell: number) =>
  position.map((v) => Math.round(v / cell)).join(",");
function areaSquared(mesh: Mesh, triangle: Triple) {
  const [a, b, c] = triangle.map((i) => mesh.vertices[i].position);
  const ab = b.map((v, i) => v - a[i]),
    ac = c.map((v, i) => v - a[i]);
  return (
    (ab[1] * ac[2] - ab[2] * ac[1]) ** 2 +
    (ab[2] * ac[0] - ab[0] * ac[2]) ** 2 +
    (ab[0] * ac[1] - ab[1] * ac[0]) ** 2
  );
}
function compact(mesh: Mesh, checkpoint = () => {}): Mesh {
  const used = new Map<number, number>(),
    vertices: Vertex[] = [],
    groups: number[] = [];
  const triangles = mesh.triangles.map(
    (triangle) =>
      triangle.map((index) => {
        checkpoint();
        let mapped = used.get(index);
        if (mapped === undefined) {
          mapped = vertices.length;
          used.set(index, mapped);
          vertices.push(mesh.vertices[index]);
          groups.push(mesh.groups?.[index] ?? -1);
        }
        return mapped;
      }) as Triple,
  );
  return { vertices, triangles, groups };
}
/** Validate and simplify the incoming patch without walking the retained room. */
function prepareAppend(
  mesh: Mesh,
  patch: SurfacePatch,
  knownVertices: Map<string, number>,
  knownTriangles: Set<string>,
) {
  if (
    patch.positions.length % 3 ||
    patch.indices.length % 3 ||
    !patch.colors ||
    patch.colors.length !== patch.positions.length
  )
    throw Error(
      "Surface geometry requires matching positions and linear vertex colors.",
    );
  for (const coordinate of patch.positions)
    if (!Number.isFinite(coordinate))
      throw Error("Surface coordinates must be finite.");
  for (const color of patch.colors)
    if (!Number.isFinite(color) || color < 0 || color > 1)
      throw Error("Linear surface colors must be between zero and one.");
  for (const index of patch.indices)
    if (index >= patch.positions.length / 3)
      throw Error("Surface triangle index is outside its vertex buffer.");
  const retained = simplifyPlanarPatch(patch);
  const vertices: Vertex[] = [],
    triangles: Triple[] = [];
  const localVertices = new Map<string, number>(),
    localTriangles = new Set<string>();
  const at = (i: number) =>
    [...retained.positions.subarray(i * 3, i * 3 + 3)] as Triple;
  const vertex = (index: number, position: Triple) => {
    const key = positionKey(position);
    let id = knownVertices.get(key) ?? localVertices.get(key);
    if (id === undefined) {
      id = mesh.vertices.length + vertices.length;
      vertices.push({
        position,
        color: [
          ...retained.colors!.subarray(index * 3, index * 3 + 3),
        ] as Triple,
      });
      localVertices.set(key, id);
    }
    return id;
  };
  for (let i = 0; i < retained.indices.length; i += 3) {
    const ids = [...retained.indices.subarray(i, i + 3)];
    const [a, b, c] = ids.map(at);
    const u = b.map((v, j) => v - a[j]),
      v = c.map((n, j) => n - a[j]);
    if (
      Math.hypot(
        u[1] * v[2] - u[2] * v[1],
        u[2] * v[0] - u[0] * v[2],
        u[0] * v[1] - u[1] * v[0],
      ) === 0
    )
      continue;
    const triangle = ids.map((id, j) => vertex(id, [a, b, c][j])) as Triple;
    const key = triangleKey(triangle);
    if (knownTriangles.has(key) || localTriangles.has(key)) continue;
    localTriangles.add(key);
    triangles.push(triangle);
  }
  return { vertices, triangles, retained, localVertices, localTriangles };
}

/** Cluster only within connected observed surfaces, never across distant gaps. */
function components(mesh: Mesh, initialCellM: number, checkpoint: () => void) {
  const parents = mesh.vertices.map((_, i) => i);
  const find = (index: number): number => {
    while (parents[index] !== index) {
      parents[index] = parents[parents[index]];
      index = parents[index];
    }
    return index;
  };
  const union = (a: number, b: number) => {
    const x = find(a),
      y = find(b);
    if (x !== y) parents[y] = x;
  };
  // Simplification may retain disconnected representatives of one originally
  // connected region. Keep that provenance without adding connecting faces.
  const priorGroups = new Map<number, number>();
  mesh.groups?.forEach((group, index) => {
    if (group < 0) return;
    const first = priorGroups.get(group);
    if (first === undefined) priorGroups.set(group, index);
    else union(first, index);
  });
  for (const [a, b, c] of mesh.triangles) {
    checkpoint();
    union(a, b);
    union(a, c);
  }
  // Permit overlapping capture grids to become one component at the starting
  // resolution. This tolerance never grows when the rendering mesh coarsens.
  const neighbors = new Map<string, number>();
  mesh.vertices.forEach((vertex, index) => {
    checkpoint();
    const key = cellKey(vertex.position, initialCellM),
      other = neighbors.get(key);
    if (other === undefined) neighbors.set(key, index);
    else union(other, index);
  });
  return parents.map((_, i) => find(i));
}
type Point2 = [number, number];
const polygonArea = (polygon: Point2[]) =>
  Math.abs(
    polygon.reduce((sum, p, i) => {
      const q = polygon[(i + 1) % polygon.length];
      return sum + p[0] * q[1] - p[1] * q[0];
    }, 0),
  ) / 2;
function splitPolygon(polygon: Point2[], a: Point2, b: Point2, sign: number) {
  const inside: Point2[] = [],
    outside: Point2[] = [];
  const distance = (p: Point2) =>
    sign * ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]));
  for (let i = 0; i < polygon.length; i++) {
    const p = polygon[i],
      q = polygon[(i + 1) % polygon.length],
      dp = distance(p),
      dq = distance(q);
    (dp >= 0 ? inside : outside).push(p);
    if (dp >= 0 !== dq >= 0) {
      const t = dp / (dp - dq),
        intersection: Point2 = [
          p[0] + t * (q[0] - p[0]),
          p[1] + t * (q[1] - p[1]),
        ];
      inside.push(intersection);
      outside.push(intersection);
    }
  }
  return { inside, outside };
}
function subtractTriangle(
  polygon: Point2[],
  triangle: Point2[],
  epsilon: number,
) {
  const [a, b, c] = triangle;
  const sign = Math.sign(
    (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]),
  );
  if (!sign) return [polygon];
  let remainder = polygon;
  const pieces: Point2[][] = [];
  for (let edge = 0; edge < 3 && remainder.length; edge++) {
    const split = splitPolygon(
      remainder,
      triangle[edge],
      triangle[(edge + 1) % 3],
      sign,
    );
    if (split.outside.length >= 3 && polygonArea(split.outside) > epsilon)
      pieces.push(split.outside);
    remainder = split.inside;
  }
  return pieces;
}
/** Fast support proof for complete axis-aligned rectangles, never a holey mesh. */
function rectangularComponents(
  mesh: Mesh,
  roots: number[],
  checkpoint: () => void,
) {
  type Region = { bounds: number[][]; vertices: number[]; triangles: Triple[] };
  const regions = new Map<number, Region>(),
    rectangles = new Set<number>();
  mesh.vertices.forEach((vertex, index) => {
    checkpoint();
    let region = regions.get(roots[index]);
    if (!region) {
      region = {
        bounds: [
          [Infinity, -Infinity],
          [Infinity, -Infinity],
          [Infinity, -Infinity],
        ],
        vertices: [],
        triangles: [],
      };
      regions.set(roots[index], region);
    }
    region.vertices.push(index);
    vertex.position.forEach((v, axis) => {
      region!.bounds[axis][0] = Math.min(region!.bounds[axis][0], v);
      region!.bounds[axis][1] = Math.max(region!.bounds[axis][1], v);
    });
  });
  for (const triangle of mesh.triangles) {
    checkpoint();
    regions.get(roots[triangle[0]])!.triangles.push(triangle);
  }
  for (const [root, region] of regions) {
    const drop = region.bounds.findIndex(([min, max]) => max - min < 1e-7);
    if (drop < 0) continue;
    const axes = [0, 1, 2].filter((axis) => axis !== drop),
      [u, v] = axes;
    const area =
      (region.bounds[u][1] - region.bounds[u][0]) *
      (region.bounds[v][1] - region.bounds[v][0]);
    if (area <= 0) continue;
    const edges = new Map<string, { count: number; a: number; b: number }>();
    let covered = 0;
    for (const triangle of region.triangles) {
      checkpoint();
      const [a, b, c] = triangle.map((i) => mesh.vertices[i].position);
      covered +=
        Math.abs(
          (b[u] - a[u]) * (c[v] - a[v]) - (b[v] - a[v]) * (c[u] - a[u]),
        ) / 2;
      for (let i = 0; i < 3; i++) {
        const a = triangle[i],
          b = triangle[(i + 1) % 3],
          key = a < b ? `${a},${b}` : `${b},${a}`,
          old = edges.get(key);
        if (old) old.count++;
        else edges.set(key, { count: 1, a, b });
      }
    }
    if (Math.abs(covered - area) > area * 1e-7) continue;
    let valid = true;
    for (const edge of edges.values()) {
      checkpoint();
      if (edge.count > 2) {
        valid = false;
        break;
      }
      if (edge.count === 2) continue;
      const a = mesh.vertices[edge.a].position,
        b = mesh.vertices[edge.b].position;
      if (
        !axes.some((axis) =>
          region.bounds[axis].some(
            (bound) =>
              Math.abs(a[axis] - bound) < 1e-7 &&
              Math.abs(b[axis] - bound) < 1e-7,
          ),
        )
      ) {
        valid = false;
        break;
      }
    }
    if (valid) rectangles.add(root);
  }
  return rectangles;
}
/** Exact planar coverage subtraction prevents a coarse triangle from filling a hole. */
function observedSupport(
  mesh: Mesh,
  roots: number[],
  cellM: number,
  tolerance: number,
  checkpoint: () => void,
) {
  const buckets = new Map<string, number[]>(),
    large: number[] = [];
  let insertions = 0;
  const bounds = mesh.triangles.map((triangle) => {
    checkpoint();
    const p = triangle.map((i) => mesh.vertices[i].position);
    return [0, 1, 2].map((axis) => [
      Math.min(...p.map((v) => v[axis])),
      Math.max(...p.map((v) => v[axis])),
    ]);
  });
  mesh.triangles.forEach((triangle, index) => {
    checkpoint();
    const range = bounds[index].map(([min, max]) => [
      Math.floor((min - tolerance) / cellM),
      Math.floor((max + tolerance) / cellM),
    ]);
    const count = range.reduce((n, [min, max]) => n * (max - min + 1), 1);
    if (count > 64) {
      large.push(index);
      return;
    }
    for (let x = range[0][0]; x <= range[0][1]; x++)
      for (let y = range[1][0]; y <= range[1][1]; y++)
        for (let z = range[2][0]; z <= range[2][1]; z++) {
          checkpoint();
          const key = `${roots[triangle[0]]}:${x},${y},${z}`,
            bucket = buckets.get(key);
          if (++insertions > 4_000_000 || (!bucket && buckets.size >= 500_000))
            throw Error(
              "Surface map capacity reached: support indexing exceeds its bounded memory budget.",
            );
          if (bucket) bucket.push(index);
          else buckets.set(key, [index]);
        }
  });
  let work = 0;
  return (triangle: Triple) => {
    checkpoint();
    if (work >= 150000 || large.length > 4096) return false;
    const p = triangle.map((i) => mesh.vertices[i].position),
      [a, b, c] = p;
    const ab = b.map((v, i) => v - a[i]),
      ac = c.map((v, i) => v - a[i]);
    const normal = [
      ab[1] * ac[2] - ab[2] * ac[1],
      ab[2] * ac[0] - ab[0] * ac[2],
      ab[0] * ac[1] - ab[1] * ac[0],
    ];
    const length = Math.hypot(...normal),
      drop = normal.map(Math.abs).indexOf(Math.max(...normal.map(Math.abs)));
    const project = (point: Triple) =>
      point.filter((_, axis) => axis !== drop) as Point2;
    const projected = p.map(project),
      epsilon = Math.max(1e-18, polygonArea(projected) * 1e-9);
    const worldBounds = [0, 1, 2].map((axis) => [
      Math.min(...p.map((v) => v[axis])),
      Math.max(...p.map((v) => v[axis])),
    ]);
    const range = worldBounds.map(([min, max]) => [
      Math.floor(min / cellM),
      Math.floor(max / cellM),
    ]);
    if (range.reduce((n, [min, max]) => n * (max - min + 1), 1) > 64)
      return false;
    const candidates = new Set(
      large.filter(
        (index) => roots[mesh.triangles[index][0]] === roots[triangle[0]],
      ),
    );
    for (let x = range[0][0]; x <= range[0][1]; x++)
      for (let y = range[1][0]; y <= range[1][1]; y++)
        for (let z = range[2][0]; z <= range[2][1]; z++)
          for (const index of buckets.get(
            `${roots[triangle[0]]}:${x},${y},${z}`,
          ) ?? [])
            candidates.add(index);
    let uncovered: Point2[][] = [projected],
      checked = 0;
    for (const index of candidates) {
      checkpoint();
      if (
        bounds[index].some(
          ([min, max], axis) =>
            max < worldBounds[axis][0] - tolerance ||
            min > worldBounds[axis][1] + tolerance,
        )
      )
        continue;
      if (++checked > 4096 || ++work > 150000) return false;
      const support = mesh.triangles[index].map(
        (i) => mesh.vertices[i].position,
      );
      if (
        support.some(
          (point) =>
            Math.abs(
              point.reduce(
                (sum, v, axis) => sum + (v - a[axis]) * normal[axis],
                0,
              ),
            ) /
              length >
            tolerance,
        )
      )
        continue;
      const clip = support.map(project);
      uncovered = uncovered.flatMap((polygon) =>
        subtractTriangle(polygon, clip, epsilon),
      );
      if (!uncovered.length) return true;
      if (uncovered.length > 64) return false;
    }
    return false;
  };
}
function coarsen(
  mesh: Mesh,
  roots: number[],
  cellM: number,
  initialCellM: number,
  representatives: number,
  checkpoint: () => void,
  locked?: (index: number) => boolean,
): Triple[] {
  const originalFaces = new Set(
    mesh.triangles.map((triangle) => {
      checkpoint();
      return triangleKey(triangle);
    }),
  );
  let supported: ReturnType<typeof observedSupport> | undefined;
  let rectangles: Set<number> | undefined;
  const observed = (triangle: Triple) => {
    if (originalFaces.has(triangleKey(triangle))) return true;
    rectangles ??= rectangularComponents(mesh, roots, checkpoint);
    if (rectangles.has(roots[triangle[0]])) return true;
    supported ??= observedSupport(
      mesh,
      roots,
      cellM,
      initialCellM / 4,
      checkpoint,
    );
    return supported(triangle);
  };
  const origins = new Map<number, Triple>();
  mesh.vertices.forEach((vertex, index) => {
    checkpoint();
    if (!origins.has(roots[index])) origins.set(roots[index], vertex.position);
  });
  const regionKey = (position: Triple, root: number) =>
    `${root}:${cellKey(position.map((v, axis) => v - origins.get(root)![axis]) as Triple, cellM)}`;
  const anchors = new Map<string, number>();
  const remap = mesh.vertices.map((vertex, index) => {
    checkpoint();
    // A vertex shared with an unprocessed block stays put, so seams stay closed.
    if (locked?.(index)) return index;
    const key = regionKey(vertex.position, roots[index]);
    let anchor = anchors.get(key);
    if (anchor === undefined) {
      anchor = index;
      anchors.set(key, index);
    }
    return anchor;
  });
  const triangles: Triple[] = [],
    keys = new Set<string>(),
    covered = new Set<string>();
  const fallbacks = new Map<string, { triangle: Triple; area: number }>();
  const retain = (triangle: Triple) => {
    const key = triangleKey(triangle);
    if (!keys.has(key)) {
      keys.add(key);
      triangles.push(triangle);
    }
  };
  // Keep observed end regions when a connected thin/holey surface collapses.
  // Two original triangles preserve near/far coverage when the budget permits;
  // no synthetic spanning triangle is introduced to represent that footprint.
  const ends = new Map<
    number,
    { first: Triple; center: number[]; far: Triple; distance: number }
  >();
  for (const triangle of mesh.triangles) {
    checkpoint();
    const root = roots[triangle[0]],
      center = [0, 1, 2].map(
        (axis) =>
          triangle.reduce(
            (sum, index) => sum + mesh.vertices[index].position[axis],
            0,
          ) / 3,
      );
    const end = ends.get(root);
    if (!end)
      ends.set(root, { first: triangle, center, far: triangle, distance: 0 });
    else {
      const distance = center.reduce(
        (sum, v, axis) => sum + (v - end.center[axis]) ** 2,
        0,
      );
      if (distance > end.distance) {
        end.far = triangle;
        end.distance = distance;
      }
    }
  }
  const regionOf = (triangle: Triple) =>
    regionKey(
      [0, 1, 2].map(
        (axis) =>
          triangle.reduce(
            (sum, index) => sum + mesh.vertices[index].position[axis],
            0,
          ) / 3,
      ) as Triple,
      roots[triangle[0]],
    );
  // A retained end already represents its own cluster region.
  for (const end of ends.values()) {
    retain(end.first);
    covered.add(regionOf(end.first));
    if (representatives > 1) {
      retain(end.far);
      covered.add(regionOf(end.far));
    }
  }
  for (const triangle of mesh.triangles) {
    checkpoint();
    const region = regionOf(triangle);
    const mapped = triangle.map((index) => remap[index]) as Triple;
    if (
      new Set(mapped).size === 3 &&
      areaSquared(mesh, mapped) > 0 &&
      observed(mapped)
    ) {
      retain(mapped);
      covered.add(region);
    } else {
      const area = areaSquared(mesh, triangle),
        old = fallbacks.get(region);
      if (!old || area > old.area) fallbacks.set(region, { triangle, area });
    }
  }
  // A small/thin observed region can fall entirely inside one cluster. Retain
  // an actual observed triangle there instead of erasing it or inventing a face.
  for (const [region, { triangle }] of fallbacks)
    if (!covered.has(region)) retain(triangle);
  return triangles;
}

/** World-aligned blocks bound each coarsening index to one local room region. */
const BLOCK_M = 1;
/** Coarsen to this share of each budget so later views append without rework. */
const LOW_WATER = 0.85;
/** Rungs double a block's grid from the initial cell; the last keeps one representative. */
const DOUBLING_RUNGS = 8;
const MAX_VISITS = 6_000_000;
/** Triangle visits after which a rebalance that already fits stops early. */
const SOFT_VISITS = 250_000;

/**
 * Cumulative colored mesh with adaptive spatial detail, never a FIFO view cache.
 * Over budget, the densest room blocks coarsen first, so later views keep
 * contributing while sparse areas keep their detail. Disconnected components
 * keep observed representatives. If even one triangle per component cannot
 * fit, add fails atomically and leaves prior coverage intact.
 */
export class PersistentSurfaceMap {
  private mesh: Mesh = { vertices: [], triangles: [] };
  private cached: SurfacePatch | null = null;
  private knownVertices = new Map<string, number>();
  private knownTriangles = new Set<string>();
  private revision = 0;
  private sentVertices = 0;
  private sentIndices = 0;
  private replaceDelta = true;
  private spacing: number;
  private clustered = false;
  private saturated = false;
  private blockRungs = new Map<string, number>();
  private nextGroup = 0;
  private retirementCursor = 0;
  private recentRetirementCursor = 0;
  private vertexUses: number[] = [];
  private orphanCount = 0;
  private retirementJournal: {
    vertices: [string, number][];
    triangles: string[];
  } | null = null;
  private readonly maxTriangles: number;
  private readonly maxVertices: number;
  private readonly initialCellM: number;
  constructor(options: PersistentSurfaceMapOptions = {}) {
    this.maxTriangles = options.maxTriangles ?? 1000000;
    this.maxVertices = options.maxVertices ?? 500000;
    this.initialCellM = options.initialCellM ?? 0.02;
    if (
      !Number.isSafeInteger(this.maxTriangles) ||
      this.maxTriangles < 1 ||
      !Number.isSafeInteger(this.maxVertices) ||
      this.maxVertices < 3 ||
      !Number.isFinite(this.initialCellM) ||
      this.initialCellM <= 0
    )
      throw Error(
        "Surface map budgets must permit at least one triangle and use a positive finite cell size.",
      );
    this.spacing = this.initialCellM;
  }
  /** Coarsest grid applied anywhere in the map; finer detail may remain elsewhere. */
  get cellM() {
    return this.clustered ? this.spacing : 0;
  }
  get triangleCount() {
    return this.mesh.triangles.length;
  }
  /** True once no retained region can coarsen further to admit new geometry. */
  get atCapacity() {
    return this.saturated;
  }
  add(patch: SurfacePatch): { retained: SurfacePatch | null; reset: boolean } {
    const addition = prepareAppend(
      this.mesh,
      patch,
      this.knownVertices,
      this.knownTriangles,
    );
    if (!addition.triangles.length)
      return { retained: addition.retained, reset: false };
    if (
      this.mesh.vertices.length + addition.vertices.length <=
        this.maxVertices &&
      this.mesh.triangles.length + addition.triangles.length <=
        this.maxTriangles
    ) {
      for (const vertex of addition.vertices) {
        this.mesh.vertices.push(vertex);
        this.vertexUses.push(0);
        this.mesh.groups?.push(-1);
      }
      for (const triangle of addition.triangles) {
        this.mesh.triangles.push(triangle);
        for (const id of triangle) this.vertexUses[id]++;
      }
      for (const [key, id] of addition.localVertices)
        this.knownVertices.set(key, id);
      for (const key of addition.localTriangles) this.knownTriangles.add(key);
      this.cached = null;
      this.revision++;
      return { retained: addition.retained, reset: false };
    }
    // Reclaim retired vertex slots only under budget pressure, not on every observation.
    if (this.orphanCount) {
      const compacted = compact(this.mesh);
      this.mesh = compacted;
      this.knownVertices = new Map(
        compacted.vertices.map((v, i) => [positionKey(v.position), i]),
      );
      this.knownTriangles = new Set(compacted.triangles.map(triangleKey));
      this.recountUses();
      this.replaceDelta = true;
      this.cached = null;
      this.revision++;
      return this.add(patch);
    }
    // A saturated map fails fast instead of repeating futile whole-map work.
    if (this.saturated)
      throw Error(
        "Surface map capacity reached: every retained region is already at its coarsest bounded detail.",
      );
    const combined: Mesh = {
      vertices: [...this.mesh.vertices, ...addition.vertices],
      triangles: [...this.mesh.triangles, ...addition.triangles],
      groups: [
        ...(this.mesh.groups ?? this.mesh.vertices.map(() => -1)),
        ...addition.vertices.map(() => -1),
      ],
    };
    // Coarsening is speculative: time/memory exhaustion leaves the previous
    // snapshot intact. Check inside costly support-index construction too.
    const deadline = performance.now() + 8_000;
    let operations = 0;
    const checkpoint = () => {
      if ((operations++ & 1023) === 0 && performance.now() > deadline)
        throw Error(
          "Surface map capacity reached: coarsening exceeded its bounded processing time.",
        );
    };
    const blocks = this.rebalance(combined, checkpoint);
    // Linear compaction of already accepted blocks is not discarded by the deadline.
    let simplified = compact({
        ...combined,
        triangles: blocks.triangles,
        groups: blocks.groups,
      }),
      spacing = Math.max(
        this.spacing,
        ...[...blocks.rungs.values()].map((rung) => this.rungCell(rung)),
      );
    if (!blocks.fits) {
      if (!blocks.exhausted)
        throw Error(
          "Surface map capacity reached: coarsening exceeded its bounded work for one integration.",
        );
      // Blocks cannot shrink further on their own (tiny budgets); coarsen the
      // whole map from its observed geometry instead.
      try {
        ({ mesh: simplified, spacing } = this.coarsenGlobally(
          combined,
          this.spacing,
          checkpoint,
        ));
      } catch (error) {
        if (!/processing time/.test((error as Error).message))
          this.saturated = true;
        throw error;
      }
    }
    this.mesh = simplified;
    this.recountUses();
    this.knownVertices = new Map(
      simplified.vertices.map((v, i) => [positionKey(v.position), i]),
    );
    this.knownTriangles = new Set(simplified.triangles.map(triangleKey));
    this.replaceDelta = true;
    this.revision++;
    this.clustered = true;
    this.spacing = spacing;
    this.blockRungs = blocks.rungs;
    this.cached = null;
    return { retained: null, reset: true };
  }
  private recountUses() {
    this.vertexUses = this.mesh.vertices.map(() => 0);
    for (const face of this.mesh.triangles)
      for (const id of face) this.vertexUses[id]++;
    this.orphanCount = 0;
  }
  /** Roll back confirmed retirement if this capture later fails before publication. */
  retirementCheckpoint(): () => void {
    const saved = {
      mesh: this.mesh,
      cached: this.cached,
      knownVertices: this.knownVertices,
      knownTriangles: this.knownTriangles,
      revision: this.revision,
      replaceDelta: this.replaceDelta,
      saturated: this.saturated,
      retirementCursor: this.retirementCursor,
      recentRetirementCursor: this.recentRetirementCursor,
      spacing: this.spacing,
      clustered: this.clustered,
      blockRungs: this.blockRungs,
      nextGroup: this.nextGroup,
      vertexUses: this.vertexUses.slice(),
      orphanCount: this.orphanCount,
    };
    const journal = {
      vertices: [] as [string, number][],
      triangles: [] as string[],
    };
    this.retirementJournal = journal;
    return () => {
      Object.assign(this, saved);
      for (const [key, id] of journal.vertices) this.knownVertices.set(key, id);
      for (const key of journal.triangles) this.knownTriangles.add(key);
      this.retirementJournal = null;
    };
  }
  commitRetirement() {
    this.retirementJournal = null;
  }
  /** Fair bounded proof work; compact only when confirmed old faces disappear. */
  retire(evidence: DepthContradiction): number {
    const triangles = this.mesh.triangles;
    if (!triangles.length) return 0;
    const removed = new Set<number>();
    const startTime = performance.now();
    const deadline = startTime + 32;
    const recentStart = Math.max(0, triangles.length - 4096);
    let visits = 0;
    const inspect = (index: number) => {
      const [a, b, c] = triangles[index];
      if (
        evidence.triangle(
          this.mesh.vertices[a].position,
          this.mesh.vertices[b].position,
          this.mesh.vertices[c].position,
        )
      )
        removed.add(index);
    };
    // Recent additions contain typical moving foreground; historic progress remains fair.
    const recentCount = triangles.length - recentStart;
    for (let i = 0; i < recentCount; i++) {
      const offset = this.recentRetirementCursor % recentCount;
      inspect(triangles.length - 1 - offset);
      this.recentRetirementCursor = (offset + 1) % recentCount;
      visits++;
      if (visits % 128 === 0 && performance.now() >= startTime + 16) break;
    }
    for (let i = 0; i < Math.min(100_000, recentStart); i++) {
      if (visits % 128 === 0 && performance.now() >= deadline) break;
      const index = this.retirementCursor % Math.max(1, recentStart);
      inspect(index);
      visits++;
      this.retirementCursor = (index + 1) % Math.max(1, recentStart);
    }
    if (!removed.size) return 0;
    // Retain stable vertex IDs and delete only affected dedup entries. Unused
    // slots are reclaimed by the existing budget path, while exports compact them.
    for (const index of removed) {
      const face = triangles[index],
        key = triangleKey(face);
      this.knownTriangles.delete(key);
      this.retirementJournal?.triangles.push(key);
      for (const id of face)
        if (--this.vertexUses[id] === 0) {
          const vertexKey = positionKey(this.mesh.vertices[id].position);
          if (this.knownVertices.get(vertexKey) === id) {
            this.knownVertices.delete(vertexKey);
            this.retirementJournal?.vertices.push([vertexKey, id]);
          }
          this.orphanCount++;
        }
    }
    const next = {
      ...this.mesh,
      triangles: triangles.filter((_, i) => !removed.has(i)),
    };
    this.mesh = next;
    this.cached = null;
    this.replaceDelta = true;
    this.revision++;
    this.saturated = false;
    this.retirementCursor %= Math.max(1, next.triangles.length);
    return removed.size;
  }
  private rungCell(rung: number) {
    return this.initialCellM * 2 ** Math.min(rung, DOUBLING_RUNGS - 1);
  }
  /** Fresh labels keep one region's provenance without aliasing another's. */
  private relabel(
    roots: number[],
    groups: number[],
    global = (i: number) => i,
  ) {
    const labels = new Map<number, number>();
    roots.forEach((root, index) => {
      let label = labels.get(root);
      if (label === undefined) labels.set(root, (label = this.nextGroup++));
      groups[global(index)] = label;
    });
  }
  /**
   * Coarsen the densest blocks one rung at a time until the map is back under
   * its low-water mark. Each support index covers one block, never the room.
   */
  private rebalance(mesh: Mesh, checkpoint: () => void) {
    const blocks = new Map<string, Triple[]>();
    for (const triangle of mesh.triangles) {
      checkpoint();
      const key = [0, 1, 2]
        .map((axis) =>
          Math.floor(
            triangle.reduce(
              (sum, index) => sum + mesh.vertices[index].position[axis],
              0,
            ) /
              3 /
              BLOCK_M,
          ),
        )
        .join(",");
      const block = blocks.get(key);
      if (block) block.push(triangle);
      else blocks.set(key, [triangle]);
    }
    const references = new Uint32Array(mesh.vertices.length),
      owners = new Int32Array(mesh.vertices.length).fill(-1),
      shared = new Uint8Array(mesh.vertices.length);
    let vertices = 0,
      triangles = mesh.triangles.length,
      id = 0;
    for (const block of blocks.values()) {
      for (const triangle of block)
        for (const index of triangle) {
          if (references[index]++ === 0) vertices++;
          if (owners[index] < 0) owners[index] = id;
          else if (owners[index] !== id) shared[index] = 1;
        }
      id++;
    }
    const groups = [...mesh.groups!],
      rungs = new Map(this.blockRungs),
      visited = new Set<string>();
    const next = (key: string) =>
      visited.has(key) ? rungs.get(key)! + 1 : Math.max(0, rungs.get(key) ?? 0);
    let visits = 0,
      exhausted = false;
    const fits = () =>
      triangles <= this.maxTriangles && vertices <= this.maxVertices;
    while (
      triangles > Math.floor(this.maxTriangles * LOW_WATER) ||
      vertices > Math.floor(this.maxVertices * LOW_WATER)
    ) {
      // Amortize: once the map fits, later integrations continue from here.
      if (visits >= SOFT_VISITS && fits()) break;
      let key: string | undefined,
        size = 0;
      for (const [candidate, block] of blocks)
        if (block.length > size && next(candidate) <= DOUBLING_RUNGS) {
          key = candidate;
          size = block.length;
        }
      if (key === undefined) {
        exhausted = true;
        break;
      }
      if ((visits += size) > MAX_VISITS) break;
      const rung = next(key),
        previous = blocks.get(key)!;
      let coarse: Triple[];
      try {
        coarse = this.coarsenBlock(
          mesh,
          previous,
          rung,
          shared,
          groups,
          checkpoint,
        );
      } catch (error) {
        // Completed blocks are valid geometry; keep them if they already fit.
        if (fits() && /processing time/.test((error as Error).message)) break;
        throw error;
      }
      visited.add(key);
      rungs.set(key, rung);
      for (const triangle of previous)
        for (const index of triangle) if (--references[index] === 0) vertices--;
      for (const triangle of coarse)
        for (const index of triangle) if (references[index]++ === 0) vertices++;
      triangles += coarse.length - previous.length;
      blocks.set(key, coarse);
    }
    return {
      triangles: [...blocks.values()].flat(),
      groups,
      rungs,
      exhausted,
      fits: fits(),
    };
  }
  private coarsenBlock(
    mesh: Mesh,
    triangles: Triple[],
    rung: number,
    shared: Uint8Array,
    groups: number[],
    checkpoint: () => void,
  ): Triple[] {
    const local = new Map<number, number>(),
      global: number[] = [];
    const block: Mesh = { vertices: [], triangles: [], groups: [] };
    for (const triangle of triangles) {
      checkpoint();
      block.triangles.push(
        triangle.map((index) => {
          let mapped = local.get(index);
          if (mapped === undefined) {
            mapped = global.length;
            local.set(index, mapped);
            global.push(index);
            block.vertices.push(mesh.vertices[index]);
            block.groups!.push(groups[index]);
          }
          return mapped;
        }) as Triple,
      );
    }
    const roots = components(block, this.initialCellM, checkpoint);
    const coarse = coarsen(
      block,
      roots,
      this.rungCell(rung),
      this.initialCellM,
      rung < DOUBLING_RUNGS ? 2 : 1,
      checkpoint,
      (index) => shared[global[index]] === 1,
    );
    this.relabel(roots, groups, (index) => global[index]);
    return coarse.map(
      (triangle) => triangle.map((index) => global[index]) as Triple,
    );
  }
  /** Whole-map fallback for small budgets where one block cannot hold detail. */
  private coarsenGlobally(
    combined: Mesh,
    start: number,
    checkpoint: () => void,
  ) {
    const roots = components(combined, this.initialCellM, checkpoint);
    const componentCount = new Set(roots).size;
    const capacity = Math.min(
      this.maxTriangles,
      Math.floor(this.maxVertices / 3),
    );
    if (componentCount > capacity)
      throw Error(
        "Surface map capacity reached: the budget cannot retain another disconnected observed region.",
      );
    const groups = [...combined.groups!];
    this.relabel(roots, groups);
    const simplify = (spacing: number) =>
      compact(
        {
          vertices: combined.vertices,
          triangles: coarsen(
            combined,
            roots,
            spacing,
            this.initialCellM,
            Math.min(2, Math.floor(capacity / componentCount)),
            checkpoint,
          ),
          groups,
        },
        checkpoint,
      );
    let spacing = start,
      simplified = simplify(spacing);
    let visits = combined.triangles.length;
    for (
      let step = 0;
      simplified.triangles.length > this.maxTriangles ||
      simplified.vertices.length > this.maxVertices;
      step++
    ) {
      visits += combined.triangles.length;
      // Bound a single fusion job as well as the retained geometry. A complex
      // fragmented input must not keep a worker busy indefinitely.
      if (step >= 12 || visits > MAX_VISITS || !Number.isFinite(spacing * 2))
        throw Error(
          "Surface map capacity reached: preserving this geometry exceeds the bounded coarsening budget.",
        );
      spacing *= 2;
      simplified = simplify(spacing);
    }
    return { mesh: simplified, spacing };
  }
  takeDelta(): SurfaceDelta {
    const vertexStart = this.replaceDelta ? 0 : this.sentVertices;
    const indexStart = this.replaceDelta ? 0 : this.sentIndices;
    const positions = new Float32Array(
      (this.mesh.vertices.length - vertexStart) * 3,
    );
    const colors = new Float32Array(positions.length);
    for (let i = vertexStart; i < this.mesh.vertices.length; i++) {
      positions.set(this.mesh.vertices[i].position, (i - vertexStart) * 3);
      colors.set(this.mesh.vertices[i].color, (i - vertexStart) * 3);
    }
    const indices = new Uint32Array(
      this.mesh.triangles.length * 3 - indexStart,
    );
    for (let i = indexStart / 3; i < this.mesh.triangles.length; i++)
      indices.set(this.mesh.triangles[i], i * 3 - indexStart);
    const delta = {
      revision: this.revision,
      reset: this.replaceDelta,
      vertexStart,
      indexStart,
      vertexCount: this.mesh.vertices.length,
      indexCount: this.mesh.triangles.length * 3,
      positions,
      colors,
      indices,
    };
    this.sentVertices = delta.vertexCount;
    this.sentIndices = delta.indexCount;
    this.replaceDelta = false;
    return delta;
  }
  snapshot(): SurfacePatch | null {
    if (!this.mesh.triangles.length) return null;
    if (!this.cached) {
      const visible = this.orphanCount ? compact(this.mesh) : this.mesh;
      this.cached = {
        id: "persistent-colored-map",
        positions: new Float32Array(
          visible.vertices.flatMap((v) => v.position),
        ),
        colors: new Float32Array(visible.vertices.flatMap((v) => v.color)),
        indices: new Uint32Array(visible.triangles.flat()),
      };
    }
    return this.cached;
  }
}
