import { useEffect, useMemo, useLayoutEffect, useRef } from "react";
import * as THREE from "three";
import type { SurfacePatch } from "./surfaceTypes";
function Patch({
  patch,
  retained,
}: {
  patch: SurfacePatch;
  retained: boolean;
}) {
  const revision = useRef(-1);
  const storage = patch.update;
  const indexArray = storage?.indices ?? patch.indices;
  const geometry = useMemo(() => {
    const result = new THREE.BufferGeometry();
    result.setAttribute(
      "position",
      new THREE.BufferAttribute(
        storage?.positions ?? patch.positions,
        3,
      ).setUsage(THREE.DynamicDrawUsage),
    );
    result.setIndex(
      new THREE.BufferAttribute(indexArray, 1).setUsage(THREE.DynamicDrawUsage),
    );
    if (patch.colors)
      result.setAttribute(
        "color",
        new THREE.BufferAttribute(storage?.colors ?? patch.colors, 3).setUsage(
          THREE.DynamicDrawUsage,
        ),
      );
    if (patch.uvs)
      result.setAttribute("uv", new THREE.BufferAttribute(patch.uvs, 2));
    return result;
    // Retained storage: a deletion swaps in a same-capacity index array, which
    // reuses this geometry and its GPU buffers instead of re-uploading vertices.
  }, [
    storage?.positions ?? patch.positions,
    storage ? storage.indices.length : patch.indices,
  ]);
  useLayoutEffect(() => {
    geometry.setDrawRange(0, patch.indices.length);
    if (!storage) return;
    if (geometry.index && geometry.index.array !== indexArray)
      geometry.index.array = indexArray;
    const continuous = revision.current + 1 === storage.revision;
    const vertexStart = continuous ? storage.vertexStart * 3 : 0;
    const indexStart = continuous ? storage.indexStart : 0;
    for (const key of ["position", "color"]) {
      const attribute = geometry.getAttribute(key) as THREE.BufferAttribute;
      if (patch.positions.length > vertexStart) {
        attribute.addUpdateRange(
          vertexStart,
          patch.positions.length - vertexStart,
        );
        attribute.needsUpdate = true;
      }
    }
    if (geometry.index && patch.indices.length > indexStart) {
      geometry.index.addUpdateRange(
        indexStart,
        patch.indices.length - indexStart,
      );
      geometry.index.needsUpdate = true;
    }
    revision.current = storage.revision;
  }, [patch, geometry, storage]);
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
    <mesh frustumCulled={false} geometry={geometry} raycast={() => null}>
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
