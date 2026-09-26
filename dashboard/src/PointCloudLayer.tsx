import { useLayoutEffect, useRef, useSyncExternalStore } from "react";
import { useThree } from "@react-three/fiber";
import { BufferGeometry, DynamicDrawUsage, type PointsMaterial } from "three";
import { PointCloudStore } from "./pointCloud";

const roundSplats: PointsMaterial["onBeforeCompile"] = (shader) => {
  shader.fragmentShader = shader.fragmentShader.replace(
    "#include <clipping_planes_fragment>",
    `
    #include <clipping_planes_fragment>
    vec2 offset = gl_PointCoord - vec2(0.5);
    if (dot(offset, offset) > 0.25) discard;
  `,
  );
};

export default function PointCloud({ cloud }: { cloud: PointCloudStore }) {
  const version = useSyncExternalStore(cloud.subscribe, cloud.snapshot);
  const geometry = useRef<BufferGeometry>(null);
  const invalidate = useThree((state) => state.invalidate);
  useLayoutEffect(() => {
    const buffer = geometry.current;
    if (!buffer) return;
    buffer.setDrawRange(0, cloud.count);
    buffer.attributes.position.needsUpdate = true;
    buffer.attributes.color.needsUpdate = true;
    invalidate();
  }, [cloud, version, invalidate]);
  return (
    <points frustumCulled={false}>
      <bufferGeometry ref={geometry}>
        <bufferAttribute
          attach="attributes-position"
          args={[cloud.positions, 3]}
          usage={DynamicDrawUsage}
        />
        <bufferAttribute
          attach="attributes-color"
          args={[cloud.colors, 3]}
          usage={DynamicDrawUsage}
        />
      </bufferGeometry>
      <pointsMaterial
        size={0.035}
        sizeAttenuation
        vertexColors
        depthTest
        depthWrite
        toneMapped={false}
        onBeforeCompile={roundSplats}
      />
    </points>
  );
}
