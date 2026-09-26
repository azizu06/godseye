import { useMemo, useSyncExternalStore } from "react";
import { DoubleSide } from "three";
import { PointCloudStore } from "./pointCloud";
import { wallVertices } from "./walls";

export default function WallLayer({ cloud }: { cloud: PointCloudStore }) {
  useSyncExternalStore(cloud.subscribe, cloud.snapshot);
  const positions = useMemo(() => wallVertices(cloud.walls), [cloud.walls]);
  if (!positions.length) return null;
  return (
    <mesh frustumCulled={false}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
      </bufferGeometry>
      <meshBasicMaterial
        color="#999c9f"
        side={DoubleSide}
        depthWrite
        polygonOffset
        polygonOffsetFactor={-1}
        polygonOffsetUnits={-2}
        toneMapped={false}
      />
    </mesh>
  );
}
