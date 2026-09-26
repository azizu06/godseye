import { useEffect, useMemo } from "react";
import * as THREE from "three";
import type { SurfacePatch } from "./surfaceTypes";
function Patch({
  patch,
  retained,
}: {
  patch: SurfacePatch;
  retained: boolean;
}) {
  const geometry = useMemo(() => {
    const result = new THREE.BufferGeometry();
    result.setAttribute(
      "position",
      new THREE.BufferAttribute(patch.positions, 3),
    );
    result.setIndex(new THREE.BufferAttribute(patch.indices, 1));
    if (patch.colors)
      result.setAttribute("color", new THREE.BufferAttribute(patch.colors, 3));
    if (patch.uvs)
      result.setAttribute("uv", new THREE.BufferAttribute(patch.uvs, 2));
    return result;
  }, [patch]);
  const texture = useMemo(() => {
    if (!patch.image) return null;
    const result = new THREE.CanvasTexture(patch.image);
    result.colorSpace = THREE.SRGBColorSpace;
    result.minFilter = THREE.LinearFilter;
    result.magFilter = THREE.LinearFilter;
    result.generateMipmaps = false;
    return result;
  }, [patch.image]);
  useEffect(() => () => geometry.dispose(), [geometry]);
  useEffect(() => () => texture?.dispose(), [texture]);
  return (
    <mesh geometry={geometry} raycast={() => null}>
      <meshBasicMaterial
        map={texture}
        vertexColors={Boolean(patch.colors)}
        side={THREE.DoubleSide}
        toneMapped={false}
        fog={false}
        polygonOffset
        polygonOffsetFactor={retained ? 4 : 1}
        polygonOffsetUnits={retained ? 4 : 1}
      />
    </mesh>
  );
}
export function ColorSurfaces({
  patches,
  retained = false,
}: {
  patches: SurfacePatch[];
  retained?: boolean;
}) {
  return (
    <group name="observed-color-surfaces">
      {patches.map((patch) => (
        <Patch key={patch.id} patch={patch} retained={retained} />
      ))}
    </group>
  );
}
