import { useEffect, useRef, type ComponentRef, type ReactNode } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { Grid, OrbitControls } from "@react-three/drei";
import { Color, DoubleSide, MOUSE, TOUCH, Vector3 } from "three";
import type { CloudBounds } from "./pointCloud";

const background = "#393939";
const initialCamera = {
  position: [9, 6, 9] as [number, number, number],
  fov: 50,
  near: 0.01,
  far: 10000,
};
const axisPositions = new Float32Array([
  -10000, 0, 0, 10000, 0, 0, 0, 0, -10000, 0, 0, 10000,
]);
const xColor = new Color("#985c5c");
const zColor = new Color("#65865c");
const axisColors = new Float32Array([
  ...xColor.toArray(),
  ...xColor.toArray(),
  ...zColor.toArray(),
  ...zColor.toArray(),
]);

function Navigation({ frameCloud }: { frameCloud?: () => CloudBounds | null }) {
  const controls = useRef<ComponentRef<typeof OrbitControls>>(null);
  const frame = useRef(frameCloud);
  frame.current = frameCloud;
  const { gl, invalidate } = useThree();

  useEffect(() => {
    const orbit = controls.current;
    if (!orbit) return;
    const canvas = gl.domElement;
    canvas.tabIndex = 0;
    canvas.setAttribute("role", "application");
    canvas.setAttribute("aria-label", "Interactive 3D viewport");
    canvas.setAttribute("aria-describedby", "navigation-help keyboard-help");
    const focus = () => canvas.focus({ preventScroll: true });
    const keyboard = (event: KeyboardEvent) => {
      if (event.altKey || event.metaKey || event.ctrlKey) return;
      if (event.key.toLowerCase() === "f") {
        const bounds = frame.current?.();
        if (!bounds) return;
        orbit.enableDamping = false;
        orbit.update();
        const direction = orbit.object.position
          .clone()
          .sub(orbit.target)
          .normalize();
        const halfFov = (25 * Math.PI) / 180;
        const fitAngle = Math.min(
          halfFov,
          Math.atan(
            (Math.tan(halfFov) * canvas.clientWidth) / canvas.clientHeight,
          ),
        );
        const distance = Math.min(
          orbit.maxDistance,
          Math.max(0.25, (bounds.radius / Math.sin(fitAngle)) * 1.15),
        );
        orbit.target.set(...bounds.center);
        orbit.object.position
          .copy(orbit.target)
          .addScaledVector(direction, distance);
        orbit.update();
        orbit.enableDamping = true;
      } else if (event.key === "Home") {
        // Drain any remaining drag inertia before restoring the saved camera.
        orbit.enableDamping = false;
        orbit.update();
        orbit.reset();
        orbit.enableDamping = true;
      } else if (event.shiftKey && event.key.startsWith("Arrow")) {
        const step = Math.PI / 36;
        if (event.key === "ArrowLeft")
          orbit.setAzimuthalAngle(orbit.getAzimuthalAngle() + step);
        if (event.key === "ArrowRight")
          orbit.setAzimuthalAngle(orbit.getAzimuthalAngle() - step);
        if (event.key === "ArrowUp")
          orbit.setPolarAngle(orbit.getPolarAngle() - step);
        if (event.key === "ArrowDown")
          orbit.setPolarAngle(orbit.getPolarAngle() + step);
      } else if (event.key.startsWith("Arrow")) {
        const horizontal =
          event.key === "ArrowLeft" || event.key === "ArrowRight";
        const direction =
          event.key === "ArrowLeft" || event.key === "ArrowDown" ? -1 : 1;
        const offset = new Vector3()
          .setFromMatrixColumn(orbit.object.matrix, horizontal ? 0 : 1)
          .multiplyScalar(orbit.getDistance() * 0.025 * direction);
        orbit.target.add(offset);
        orbit.object.position.add(offset);
        orbit.update();
      } else if (event.key === "+" || event.key === "=") {
        orbit.dollyIn();
        orbit.update();
      } else if (event.key === "-" || event.key === "_") {
        orbit.dollyOut();
        orbit.update();
      } else return;
      event.preventDefault();
      event.stopImmediatePropagation();
      invalidate();
    };
    orbit.saveState();
    canvas.addEventListener("pointerdown", focus);
    canvas.addEventListener("keydown", keyboard);
    return () => {
      canvas.removeEventListener("pointerdown", focus);
      canvas.removeEventListener("keydown", keyboard);
    };
  }, [gl, invalidate]);

  return (
    <OrbitControls
      ref={controls}
      makeDefault
      enableDamping
      dampingFactor={0.12}
      rotateSpeed={0.7}
      panSpeed={0.8}
      zoomSpeed={0.9}
      screenSpacePanning
      minDistance={0.1}
      maxDistance={2000}
      mouseButtons={{ LEFT: MOUSE.PAN, RIGHT: MOUSE.ROTATE, MIDDLE: MOUSE.PAN }}
      touches={{ ONE: TOUCH.ROTATE, TWO: TOUCH.DOLLY_PAN }}
    />
  );
}

/** Point clouds and future meshes share ARKit Y-up meters and a freely navigable camera. */
export default function Scene({
  children,
  frameCloud,
}: {
  children?: ReactNode;
  frameCloud?: () => CloudBounds | null;
}) {
  return (
    <Canvas
      flat
      frameloop="demand"
      dpr={[1, 2]}
      camera={initialCamera}
      gl={{ antialias: true, alpha: false }}
      onContextMenu={(event) => event.preventDefault()}
      fallback={
        <div className="viewport-fallback">
          Enable WebGL to open the 3D viewport.
        </div>
      }
    >
      <color attach="background" args={[background]} />
      <Grid
        position={[0, -0.002, 0]}
        args={[10, 10]}
        infiniteGrid
        followCamera
        side={DoubleSide}
        cellSize={1}
        cellThickness={0.65}
        cellColor="#4d4d4d"
        sectionSize={10}
        sectionThickness={0.9}
        sectionColor="#616161"
        fadeDistance={160}
        fadeStrength={1.5}
      />
      <lineSegments frustumCulled={false}>
        <bufferGeometry>
          <bufferAttribute
            attach="attributes-position"
            args={[axisPositions, 3]}
          />
          <bufferAttribute attach="attributes-color" args={[axisColors, 3]} />
        </bufferGeometry>
        <lineBasicMaterial
          vertexColors
          toneMapped={false}
          transparent
          opacity={0.8}
        />
      </lineSegments>
      {children}
      <Navigation frameCloud={frameCloud} />
    </Canvas>
  );
}
