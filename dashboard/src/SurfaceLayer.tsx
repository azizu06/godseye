import { memo, useEffect, useLayoutEffect, useMemo } from "react";
import { useThree } from "@react-three/fiber";
import {
  BufferGeometry,
  BufferAttribute,
  Texture,
  SRGBColorSpace,
  LinearMipmapLinearFilter,
  LinearFilter,
  DoubleSide,
} from "three";
import type { SurfacePatch } from "./surfaceTypes";
import type { RetainedSurfaceTile } from "./surfaceTileBuffer";
import { createTileGeometry, syncTileGeometry } from "./surfaceTileGeometry";

const RetainedTile = memo(function RetainedTile({
  tile,
}: {
  tile: RetainedSurfaceTile;
}) {
  const { buffer } = tile;
  const invalidate = useThree((state) => state.invalidate);
  const geometry = useMemo(
    () => createTileGeometry(buffer),
    [buffer.positions, buffer.colors, buffer.indices],
  );
  useLayoutEffect(() => {
    syncTileGeometry(geometry, buffer);
    invalidate();
  }, [tile, geometry, buffer, invalidate]);
  useEffect(() => () => geometry.dispose(), [geometry]);
  return (
    <mesh geometry={geometry} raycast={() => {}}>
      <meshBasicMaterial
        vertexColors
        side={DoubleSide}
        toneMapped={false}
        polygonOffset
        polygonOffsetFactor={2}
        polygonOffsetUnits={2}
      />
    </mesh>
  );
});

const Patch = memo(function Patch({ patch }: { patch: SurfacePatch }) {
  const { gl, invalidate } = useThree();
  const geometry = useMemo(() => {
    const mesh = new BufferGeometry();
    mesh.setAttribute("position", new BufferAttribute(patch.positions, 3));
    mesh.setIndex(new BufferAttribute(patch.indices, 1));
    if (patch.colors)
      mesh.setAttribute("color", new BufferAttribute(patch.colors, 3));
    if (patch.uvs) mesh.setAttribute("uv", new BufferAttribute(patch.uvs, 2));
    mesh.computeBoundingSphere();
    return mesh;
  }, [patch]);
  const texture = useMemo(() => {
    if (!patch.image) return null;
    const texture = new Texture(patch.image);
    texture.flipY = false; // Worker already flipped ImageBitmap pixels.
    texture.colorSpace = SRGBColorSpace;
    texture.minFilter = LinearMipmapLinearFilter;
    texture.magFilter = LinearFilter;
    texture.anisotropy = Math.min(4, gl.capabilities.getMaxAnisotropy());
    texture.needsUpdate = true;
    return texture;
  }, [patch.image, gl]);
  useEffect(() => {
    invalidate();
    return () => geometry.dispose();
  }, [geometry, invalidate]);
  useEffect(() => () => texture?.dispose(), [texture]);
  return (
    <mesh geometry={geometry} raycast={() => {}}>
      <meshBasicMaterial
        map={texture}
        vertexColors={Boolean(patch.colors)}
        side={DoubleSide}
        toneMapped={false}
        polygonOffset
        polygonOffsetFactor={-1}
        polygonOffsetUnits={-1}
      />
    </mesh>
  );
});

export default function SurfaceLayer({
  tiles,
  recent,
}: {
  tiles: RetainedSurfaceTile[];
  recent: SurfacePatch[];
}) {
  return (
    <group name="observed-surfaces">
      {tiles.map((tile) => (
        <RetainedTile key={`tile:${tile.buffer.id}`} tile={tile} />
      ))}
      {recent.map((patch) => (
        <Patch key={`view:${patch.id}`} patch={patch} />
      ))}
    </group>
  );
}
