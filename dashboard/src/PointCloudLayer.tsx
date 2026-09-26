import { memo, useEffect, useRef } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import {
  BufferAttribute,
  BufferGeometry,
  DynamicDrawUsage,
  type PointsMaterial,
} from "three";
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

export default memo(function PointCloud({ cloud }: { cloud: PointCloudStore }) {
  const uploaded = useRef(-1);
  const geometry = useRef<BufferGeometry>(null);
  const invalidate = useThree((state) => state.invalidate);
  useEffect(() => cloud.subscribe(invalidate), [cloud, invalidate]);
  useFrame(() => {
    const version = cloud.snapshot();
    if (version === uploaded.current) return;
    const buffer = geometry.current;
    if (!buffer) return;
    uploaded.current = version;
    buffer.setDrawRange(0, cloud.count);
    const ranges = cloud.takeUpdateRanges();
    for (const attribute of [
      buffer.attributes.position,
      buffer.attributes.color,
    ]) {
      if (!(attribute instanceof BufferAttribute)) continue;
      for (const range of ranges)
        attribute.addUpdateRange(range.start, range.count);
      if (ranges.length) attribute.needsUpdate = true;
    }
  });
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
});
