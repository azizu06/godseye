import {
  BufferAttribute,
  BufferGeometry,
  DynamicDrawUsage,
  Sphere,
  Vector3,
} from "three";
import {
  SurfaceTileBuffer,
  mergeUploadRanges,
  type UploadRange,
} from "./surfaceTileBuffer";

export function createTileGeometry(buffer: SurfaceTileBuffer) {
  const geometry = new BufferGeometry();
  geometry.setAttribute(
    "position",
    new BufferAttribute(buffer.positions, 3).setUsage(DynamicDrawUsage),
  );
  geometry.setAttribute(
    "color",
    new BufferAttribute(buffer.colors, 3).setUsage(DynamicDrawUsage),
  );
  geometry.setIndex(
    new BufferAttribute(buffer.indices, 1).setUsage(DynamicDrawUsage),
  );
  return geometry;
}

function upload(attribute: BufferAttribute, ranges: UploadRange[]) {
  if (!ranges.length) return;
  // Rendering can pause while worker messages continue. Bound pending uploads by
  // buffer coverage, and retain every change until WebGL consumes the ranges.
  const merged = mergeUploadRanges([...attribute.updateRanges, ...ranges]);
  attribute.clearUpdateRanges();
  for (const range of merged)
    attribute.addUpdateRange(range.start, range.count);
  attribute.needsUpdate = true;
}

export function syncTileGeometry(
  geometry: BufferGeometry,
  buffer: SurfaceTileBuffer,
) {
  geometry.setDrawRange(0, buffer.indexCount);
  // Worker bounds include only observed vertices, never unused capacity slots.
  geometry.boundingSphere = new Sphere(
    new Vector3(...buffer.bounds.center),
    buffer.bounds.radius,
  );
  const ranges = buffer.takeUploadRanges();
  upload(geometry.getAttribute("position") as BufferAttribute, ranges.vertices);
  upload(geometry.getAttribute("color") as BufferAttribute, ranges.vertices);
  upload(geometry.index!, ranges.indices);
}
