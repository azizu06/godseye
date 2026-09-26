import { memo, useEffect, useMemo, useRef } from "react";
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

export default memo(function PointCloud({
  cloud,
  visible = true,
  surfaceOcclusion = false,
}: {
  cloud: PointCloudStore;
  visible?: boolean;
  surfaceOcclusion?: boolean;
}) {
  const uploaded = useRef(-1);
  const geometry = useRef<BufferGeometry>(null);
  const mode = useRef<boolean | undefined>(undefined);
  const drawIndices = useMemo(
    () =>
      new BufferAttribute(cloud.visibleIndices, 1).setUsage(DynamicDrawUsage),
    [cloud],
  );
  const invalidate = useThree((state) => state.invalidate);
  const depthBias = useMemo(() => ({ value: 0 }), []);
  const compile = useMemo<PointsMaterial["onBeforeCompile"]>(
    () => (shader, renderer) => {
      roundSplats(shader, renderer);
      shader.uniforms.surfaceDepthBias = depthBias;
      shader.vertexShader =
        `uniform float surfaceDepthBias;\n${shader.vertexShader}`.replace(
          "#include <project_vertex>",
          `#include <project_vertex>
      // Bias only raster depth, preserving measured positions and screen XY.
      // Confirmed faces cover coplanar splats; openings and foreground stay visible.
      vec4 biasedDepth = projectionMatrix * vec4(mvPosition.xyz - vec3(0.0, 0.0, surfaceDepthBias), 1.0);
      gl_Position.z = biasedDepth.z / biasedDepth.w * gl_Position.w;`,
        );
    },
    [depthBias],
  );
  useEffect(() => {
    depthBias.value = surfaceOcclusion ? 0.005 : 0;
    invalidate();
  }, [surfaceOcclusion, depthBias, invalidate]);
  useEffect(() => cloud.subscribe(invalidate), [cloud, invalidate]);
  useFrame(() => {
    if (!visible) return;
    const version = cloud.snapshot();
    if (version === uploaded.current && mode.current === surfaceOcclusion)
      return;
    const buffer = geometry.current;
    if (!buffer) return;
    uploaded.current = version;
    mode.current = surfaceOcclusion;
    buffer.setIndex(surfaceOcclusion ? drawIndices : null);
    buffer.setDrawRange(0, surfaceOcclusion ? cloud.visibleCount : cloud.count);
    if (surfaceOcclusion) {
      const ranges = cloud.takeVisibleRanges();
      for (const range of ranges)
        drawIndices.addUpdateRange(range.start, range.count);
      if (ranges.length) drawIndices.needsUpdate = true;
    }
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
    <points name="live-point-cloud" frustumCulled={false} visible={visible}>
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
        onBeforeCompile={compile}
        customProgramCacheKey={() =>
          "round-measured-points-with-surface-depth-v1"
        }
      />
    </points>
  );
});
