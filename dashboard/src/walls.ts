export type WallRectangle = { id: string; corners: number[] };
export type WallSnapshot = {
  version: 2;
  type: "walls";
  available: true;
  session_id: string;
  map_epoch: number;
  t_capture: number;
  walls: WallRectangle[];
};

export function parseWalls(value: unknown): WallSnapshot | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as Record<string, unknown>;
  if (
    v.version !== 2 ||
    v.type !== "walls" ||
    v.available !== true ||
    typeof v.session_id !== "string" ||
    !v.session_id.length ||
    v.session_id.length > 128 ||
    typeof v.map_epoch !== "number" ||
    !Number.isSafeInteger(v.map_epoch) ||
    v.map_epoch < 1 ||
    typeof v.t_capture !== "number" ||
    !Number.isFinite(v.t_capture) ||
    v.t_capture < 0 ||
    !Array.isArray(v.walls) ||
    v.walls.length > 128
  )
    return null;
  const ids = new Set<string>();
  for (const wall of v.walls) {
    if (
      !wall ||
      typeof wall !== "object" ||
      typeof wall.id !== "string" ||
      !wall.id.length ||
      wall.id.length > 128 ||
      ids.has(wall.id) ||
      !Array.isArray(wall.corners) ||
      wall.corners.length !== 12 ||
      !wall.corners.every(
        (n: unknown) =>
          typeof n === "number" && Number.isFinite(n) && Math.abs(n) <= 1e6,
      )
    )
      return null;
    ids.add(wall.id);
  }
  if (wallRegions(v.walls as WallRectangle[]).length !== v.walls.length)
    return null;
  return value as WallSnapshot;
}

export function wallVertices(walls: WallRectangle[]) {
  const vertices = new Float32Array(walls.length * 18);
  walls.forEach((wall, i) => {
    [0, 1, 2, 0, 2, 3].forEach((corner, j) => {
      vertices.set(
        wall.corners.slice(corner * 3, corner * 3 + 3),
        i * 18 + j * 3,
      );
    });
  });
  return vertices;
}

export const WALL_DISTANCE = 0.02;
export function wallRegions(walls: WallRectangle[]) {
  return walls.flatMap(({ corners: c }) => {
    const u = [c[3] - c[0], c[4] - c[1], c[5] - c[2]];
    const v = [c[9] - c[0], c[10] - c[1], c[11] - c[2]];
    const width = Math.hypot(...u),
      height = Math.hypot(...v);
    if (width < 0.05 || height < 0.05 || width > 100.01 || height > 100.01)
      return [];
    if (
      [0, 1, 2].some(
        (i) => Math.abs(c[6 + i] - c[3 + i] - c[9 + i] + c[i]) > 0.001,
      )
    )
      return [];
    for (let i = 0; i < 3; i++) {
      u[i] /= width;
      v[i] /= height;
    }
    if (Math.abs(u[0] * v[0] + u[1] * v[1] + u[2] * v[2]) > 0.01) return [];
    const normal = [
      u[1] * v[2] - u[2] * v[1],
      u[2] * v[0] - u[0] * v[2],
      u[0] * v[1] - u[1] * v[0],
    ];
    if (Math.abs(normal[1]) > 0.2) return [];
    return [{ origin: c.slice(0, 3), u, v, normal, width, height }];
  });
}

export function onWall(
  x: number,
  y: number,
  z: number,
  regions: ReturnType<typeof wallRegions>,
) {
  for (const { origin, u, v, normal, width, height } of regions) {
    const dx = x - origin[0],
      dy = y - origin[1],
      dz = z - origin[2];
    if (
      Math.abs(dx * normal[0] + dy * normal[1] + dz * normal[2]) > WALL_DISTANCE
    )
      continue;
    const across = dx * u[0] + dy * u[1] + dz * u[2];
    const up = dx * v[0] + dy * v[1] + dz * v[2];
    // Preserve borders and adjacent objects. Only the measured interior is simplified.
    if (
      across >= WALL_DISTANCE &&
      across <= width - WALL_DISTANCE &&
      up >= WALL_DISTANCE &&
      up <= height - WALL_DISTANCE
    )
      return true;
  }
  return false;
}
