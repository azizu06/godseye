import { createPortal } from "react-dom";
import { ColorSurfaces } from "./ColorSurfaces";
import type { SurfacePatch, SurfaceStatus } from "./surfaceTypes";
import {
  Component,
  Suspense,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { Edges, Grid, Line, OrbitControls } from "@react-three/drei";
import * as THREE from "three";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import {
  Crosshair,
  HelpCircle,
  Layers3,
  Maximize,
  MousePointer2,
  ScanLine,
} from "lucide-react";
import type { Mission } from "./state";
import { decodeCells, type WorldObject } from "./protocol";
import { ProjectLabels, useSceneLabels } from "./SceneLabels";
import { LIDAR_RANGE_M, scopeArc, scopeTriangles } from "./sensorProfile";

export const objectName = (o: WorldObject) =>
  o.class === "potted plant"
    ? "Plant"
    : o.class.charAt(0).toUpperCase() + o.class.slice(1);
export interface SceneProps {
  toolsHost?: HTMLElement | null;
  feedLabel: string;
  surfaceReason: string;
  surfaces: SurfacePatch[];
  surfaceStatus: SurfaceStatus;
  persistentSurface: SurfacePatch | null;
  mapCellM: number;
  mission: Mission;
  selected: string | null;
  onSelect: (id: string) => void;
  canGoal: boolean;
  onGoal: (x: number, z: number) => void;
}
interface Layers {
  surfaces: boolean;
  points: boolean;
  objects: boolean;
  trajectory: boolean;
  occupancy: boolean;
}

function Controls({
  reset,
  frame,
  bounds,
  map,
}: {
  reset: number;
  frame: number;
  bounds: THREE.Sphere | null;
  map: string | null;
}) {
  const ref = useRef<OrbitControlsImpl>(null);
  const { camera, size, invalidate } = useThree();
  const framed = useRef<string | null>(null);
  const fit = () => {
    const controls = ref.current;
    if (!bounds || !controls || !(camera instanceof THREE.PerspectiveCamera))
      return;
    // Drain damping before moving target; framing is visualization only.
    controls.enableDamping = false;
    controls.update();
    const direction = camera.position.clone().sub(controls.target).normalize();
    const vertical = THREE.MathUtils.degToRad(camera.fov / 2);
    const angle = Math.min(
      vertical,
      Math.atan((Math.tan(vertical) * size.width) / size.height),
    );
    const distance = Math.max(0.25, (bounds.radius / Math.sin(angle)) * 1.15);
    controls.target.copy(bounds.center);
    camera.position.copy(bounds.center).addScaledVector(direction, distance);
    camera.far = Math.max(100, distance + bounds.radius * 3);
    camera.updateProjectionMatrix();
    controls.update();
    controls.enableDamping = true;
    invalidate();
  };
  const latestFit = useRef(fit);
  latestFit.current = fit;
  useEffect(() => {
    camera.position.set(7.3, 6.5, 8.2);
    ref.current?.target.set(0, 0.2, 0);
    ref.current?.update();
    invalidate();
  }, [camera, reset, invalidate]);
  useEffect(() => {
    if (frame) latestFit.current();
  }, [frame]);
  useEffect(() => {
    if (bounds && map && framed.current !== map) {
      latestFit.current();
      framed.current = map;
    }
  }, [bounds, map]);
  return (
    <OrbitControls
      ref={ref}
      makeDefault
      minDistance={0.1}
      maxDistance={2000}
      enableDamping
      dampingFactor={0.12}
      mouseButtons={{
        LEFT: THREE.MOUSE.PAN,
        MIDDLE: THREE.MOUSE.ROTATE,
        RIGHT: THREE.MOUSE.ROTATE,
      }}
      touches={{ ONE: THREE.TOUCH.ROTATE, TWO: THREE.TOUCH.DOLLY_PAN }}
    />
  );
}
/** Fixed GPU storage; append only changed ranges, compact only on bounded eviction. */
function Cloud({ mission }: { mission: Mission }) {
  const { invalidate } = useThree();
  const previous = useRef<Mission["chunks"]>([]);
  const geometry = useMemo(() => {
    const value = new THREE.BufferGeometry();
    for (const name of ["position", "color"])
      value.setAttribute(
        name,
        new THREE.BufferAttribute(new Float32Array(500000 * 3), 3).setUsage(
          THREE.DynamicDrawUsage,
        ),
      );
    value.setDrawRange(0, 0);
    return value;
  }, []);
  useLayoutEffect(() => {
    const chunks = mission.chunks;
    const appended = previous.current.every((chunk, i) => chunks[i] === chunk);
    const start = appended ? previous.current.length : 0;
    let offset = appended
      ? previous.current.reduce((n, c) => n + c.positions.length, 0)
      : 0;
    const first = offset;
    const positions = geometry.getAttribute(
      "position",
    ) as THREE.BufferAttribute;
    const colors = geometry.getAttribute("color") as THREE.BufferAttribute;
    for (let i = start; i < chunks.length; i++) {
      (positions.array as Float32Array).set(chunks[i].positions, offset);
      (colors.array as Float32Array).set(chunks[i].colors, offset);
      offset += chunks[i].positions.length;
    }
    if (offset > first) {
      for (const attribute of [positions, colors]) {
        attribute.addUpdateRange(first, offset - first);
        attribute.needsUpdate = true;
      }
    }
    geometry.setDrawRange(0, offset / 3);
    previous.current = chunks;
    invalidate();
  }, [mission.chunks, geometry, invalidate]);
  useEffect(() => () => geometry.dispose(), [geometry]);
  return (
    <points name="live-point-cloud" geometry={geometry} frustumCulled={false}>
      <pointsMaterial
        size={0.023}
        toneMapped={false}
        vertexColors
        transparent
        opacity={0.8}
        sizeAttenuation
        depthWrite={false}
        fog={false}
      />
    </points>
  );
}
function OccupancyMesh({ mission }: { mission: Mission }) {
  const grid = mission.occupancy;
  const texture = useMemo(() => {
    if (!grid) return null;
    const cells = decodeCells(grid),
      bytes = new Uint8Array(cells.length * 4);
    cells.forEach((value, i) => {
      const c =
        value === 2
          ? [227, 170, 104, 170]
          : value === 1
            ? [70, 135, 129, 45]
            : [65, 77, 100, 60];
      bytes.set(c, i * 4);
    });
    const t = new THREE.DataTexture(bytes, grid.width, grid.height);
    t.flipY = true;
    t.needsUpdate = true;
    return t;
  }, [grid]);
  useEffect(() => () => texture?.dispose(), [texture]);
  if (!grid || !texture) return null;
  return (
    <mesh
      position={[
        grid.origin[0] + (grid.width * grid.cell_m) / 2,
        0.025,
        grid.origin[1] + (grid.height * grid.cell_m) / 2,
      ]}
      rotation={[-Math.PI / 2, 0, 0]}
    >
      <planeGeometry
        args={[grid.width * grid.cell_m, grid.height * grid.cell_m]}
      />
      <meshBasicMaterial map={texture} transparent side={THREE.DoubleSide} />
    </mesh>
  );
}
function ViewScope({ mission }: { mission: Mission }) {
  const pose = mission.pose;
  const fan = useMemo(() => scopeTriangles(), []);
  const arc = useMemo(() => scopeArc(), []);
  if (!pose) return null;
  return (
    <group
      position={[pose.position[0], 0.045, pose.position[2]]}
      rotation={[0, pose.yaw_rad, 0]}
    >
      <mesh>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" args={[fan, 3]} />
        </bufferGeometry>
        <meshBasicMaterial
          color="#75e7d5"
          transparent
          opacity={0.075}
          side={THREE.DoubleSide}
          depthWrite={false}
        />
      </mesh>
      <Line
        points={[[0, 0, 0], arc[0]]}
        color="#77d5c8"
        lineWidth={0.8}
        transparent
        opacity={0.4}
      />
      <Line
        points={[[0, 0, 0], arc[arc.length - 1]]}
        color="#77d5c8"
        lineWidth={0.8}
        transparent
        opacity={0.4}
      />
      {[1, 3, LIDAR_RANGE_M].map((radius) => (
        <Line
          key={radius}
          points={scopeArc(radius)}
          color="#77d5c8"
          lineWidth={radius === LIDAR_RANGE_M ? 1 : 0.6}
          transparent
          opacity={radius === LIDAR_RANGE_M ? 0.55 : 0.25}
        />
      ))}
      <Line
        points={[
          [0, 0, 0.3],
          [0, 0, LIDAR_RANGE_M],
        ]}
        color="#8de6d4"
        lineWidth={0.7}
        dashed
        dashSize={0.08}
        gapSize={0.1}
        transparent
        opacity={0.35}
      />
    </group>
  );
}
function Trajectory({ mission }: { mission: Mission }) {
  const colors = useMemo(
    () =>
      mission.trajectory.map((_, i) =>
        new THREE.Color("#233e46").lerp(
          new THREE.Color("#88d7c6"),
          i / Math.max(1, mission.trajectory.length - 1),
        ),
      ),
    [mission.trajectory],
  );
  if (mission.trajectory.length < 2) return null;
  return (
    <Line
      points={mission.trajectory.map((p) => [p[0], 0.055, p[2]])}
      vertexColors={colors}
      lineWidth={1.5}
      transparent
      opacity={0.65}
    />
  );
}
function World({
  persistentSurface,
  surfaces,
  mission,
  selected,
  layers,
  canGoal,
  onGoal,
}: {
  persistentSurface: SurfacePatch | null;
  surfaces: SurfacePatch[];
  mission: Mission;
  selected: string | null;
  layers: Layers;
  canGoal: boolean;
  onGoal: (x: number, z: number) => void;
}) {
  const selectedEvent = mission.events
    .filter(
      (e) =>
        (e.object_id === selected || e.new_object_id === selected) &&
        (e.kind === "moved" || e.kind === "possible_move"),
    )
    .at(-1);
  return (
    <>
      {canGoal && (
        <mesh
          rotation={[-Math.PI / 2, 0, 0]}
          position={[0, -0.01, 0]}
          onClick={(e) => {
            if (e.button === 0 && e.delta < 4) onGoal(e.point.x, e.point.z);
          }}
        >
          <planeGeometry args={[40, 40]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
      )}
      <ambientLight intensity={1.5} />
      <directionalLight position={[4, 8, 3]} intensity={2} />
      <Grid
        position={[0, -0.025, 0]}
        args={[26, 26]}
        cellSize={0.5}
        cellThickness={0.5}
        cellColor="#233d48"
        sectionSize={2}
        sectionThickness={0.8}
        sectionColor="#375361"
        fadeDistance={25}
        fadeStrength={1.6}
        infiniteGrid
      />
      {layers.surfaces && persistentSurface && (
        <ColorSurfaces patches={[persistentSurface]} retained />
      )}
      {layers.surfaces && <ColorSurfaces patches={surfaces} />}
      {layers.points && <Cloud mission={mission} />}
      {layers.occupancy && <OccupancyMesh mission={mission} />}
      {layers.trajectory && <Trajectory mission={mission} />}
      {mission.path.length > 1 && (
        <Line
          points={mission.path.map((p) => [p[0], 0.075, p[1]])}
          color="#87e9db"
          lineWidth={2.5}
          dashed
          dashSize={0.1}
          gapSize={0.08}
        />
      )}
      {selectedEvent?.old_position && selectedEvent.new_position && (
        <group>
          <Line
            points={[selectedEvent.old_position, selectedEvent.new_position]}
            color="#e9b373"
            dashed
            dashSize={0.08}
            gapSize={0.05}
            lineWidth={2}
          />
          <mesh
            position={selectedEvent.old_position}
            rotation={[-Math.PI / 2, 0, 0]}
          >
            <ringGeometry args={[0.16, 0.19, 32]} />
            <meshBasicMaterial
              color="#e9b373"
              transparent
              opacity={0.7}
              side={THREE.DoubleSide}
            />
          </mesh>
        </group>
      )}
      {layers.objects &&
        mission.objects.map((o) => (
          <group key={o.id} position={o.position}>
            <mesh>
              <boxGeometry
                args={
                  o.class === "backpack"
                    ? [0.32, 0.48, 0.23]
                    : o.class === "laptop"
                      ? [0.45, 0.05, 0.3]
                      : o.class === "chair"
                        ? [0.42, 0.7, 0.42]
                        : o.class === "bottle"
                          ? [0.12, 0.3, 0.12]
                          : [0.35, 0.65, 0.35]
                }
              />
              <meshStandardMaterial
                color={o.id === selected ? "#a6efe4" : "#608991"}
                transparent
                opacity={o.id === selected ? 0.28 : 0.12}
              />
              <Edges
                color={
                  o.state === "moved"
                    ? "#e9b373"
                    : o.id === selected
                      ? "#b0f4e7"
                      : "#83bdc3"
                }
              />
            </mesh>
            <Line
              points={[
                [0, 0.1, 0],
                [0, 0.72, 0],
              ]}
              color={o.state === "moved" ? "#e9b373" : "#78b9b8"}
              transparent
              opacity={0.6}
            />
          </group>
        ))}
      <ViewScope mission={mission} />
      {mission.pose && (
        <group
          position={[mission.pose.position[0], 0.1, mission.pose.position[2]]}
          rotation={[0, mission.pose.yaw_rad, 0]}
        >
          <mesh rotation={[-Math.PI / 2, 0, 0]}>
            <ringGeometry args={[0.2, 0.23, 48]} />
            <meshBasicMaterial color="#7ee8d5" side={THREE.DoubleSide} />
          </mesh>
          <mesh>
            <boxGeometry args={[0.25, 0.12, 0.35]} />
            <meshStandardMaterial
              color="#9ecbc3"
              metalness={0.6}
              roughness={0.3}
              emissive="#274e48"
            />
            <Edges color="#b5f4e4" />
          </mesh>
          <mesh position={[0, 0.16, 0]} rotation={[-0.15, 0, 0]}>
            <boxGeometry args={[0.12, 0.2, 0.035]} />
            <meshStandardMaterial color="#1e4848" />
            <Edges color="#a0f4e2" />
          </mesh>
          <mesh position={[0, 0.02, 0.33]} rotation={[Math.PI / 2, 0, 0]}>
            <coneGeometry args={[0.1, 0.2, 3]} />
            <meshBasicMaterial color="#b3ffed" />
          </mesh>
        </group>
      )}
    </>
  );
}
export function Map2D({
  mission,
  selected,
  onSelect,
  canGoal,
  onGoal,
}: SceneProps) {
  const grid = mission.occupancy;
  const cells = useMemo(
    () => (grid ? decodeCells(grid) : new Uint8Array()),
    [grid],
  );
  const all = [
    ...mission.objects.map((o) => [o.position[0], o.position[2]]),
    ...mission.path,
    ...(mission.pose
      ? scopeArc().map(([x, , z]) => {
          const p = mission.pose!;
          return [
            p.position[0] + x * Math.cos(p.yaw_rad) + z * Math.sin(p.yaw_rad),
            p.position[2] - x * Math.sin(p.yaw_rad) + z * Math.cos(p.yaw_rad),
          ];
        })
      : []),
    ...(mission.pose
      ? [[mission.pose.position[0], mission.pose.position[2]]]
      : []),
  ];
  const minX =
      Math.min(-4, ...all.map((p) => p[0]), grid?.origin[0] ?? 0) - 0.6,
    maxX =
      Math.max(
        4,
        ...all.map((p) => p[0]),
        grid ? grid.origin[0] + grid.width * grid.cell_m : 0,
      ) + 0.6;
  const minZ =
      Math.min(-3, ...all.map((p) => p[1]), grid?.origin[1] ?? 0) - 0.6,
    maxZ =
      Math.max(
        3,
        ...all.map((p) => p[1]),
        grid ? grid.origin[1] + grid.height * grid.cell_m : 0,
      ) + 0.6;
  const last = mission.events
    .filter(
      (e) =>
        (e.object_id === selected || e.new_object_id === selected) &&
        e.old_position &&
        e.new_position,
    )
    .at(-1);
  return (
    <svg
      className={`map2d ${canGoal ? "goal-cursor" : ""}`}
      aria-label="Top-down occupancy map"
      viewBox={`${minX} ${minZ} ${maxX - minX} ${maxZ - minZ}`}
      onClick={(e) => {
        if (!canGoal) return;
        const svg = e.currentTarget,
          point = svg.createSVGPoint();
        point.x = e.clientX;
        point.y = e.clientY;
        const matrix = svg.getScreenCTM();
        if (matrix) {
          const p = point.matrixTransform(matrix.inverse());
          onGoal(p.x, p.y);
        }
      }}
    >
      <defs>
        <pattern
          id="map-grid"
          width=".5"
          height=".5"
          patternUnits="userSpaceOnUse"
        >
          <path
            d="M .5 0 H 0 V .5"
            fill="none"
            stroke="#233641"
            strokeWidth=".012"
          />
        </pattern>
      </defs>
      <rect
        x={minX}
        y={minZ}
        width={maxX - minX}
        height={maxZ - minZ}
        fill="url(#map-grid)"
      />
      {grid &&
        Array.from(cells).map(
          (c, i) =>
            c !== 0 && (
              <rect
                key={i}
                x={grid.origin[0] + (i % grid.width) * grid.cell_m}
                y={grid.origin[1] + Math.floor(i / grid.width) * grid.cell_m}
                width={grid.cell_m}
                height={grid.cell_m}
                fill={c === 2 ? "#b98d60" : "#326f67"}
                opacity={c === 2 ? 0.8 : 0.2}
              />
            ),
        )}
      {mission.trajectory.slice(1).map((p, i) => (
        <line
          key={i}
          x1={mission.trajectory[i][0]}
          y1={mission.trajectory[i][2]}
          x2={p[0]}
          y2={p[2]}
          stroke="#88d7c6"
          strokeOpacity={0.12 + (0.5 * i) / mission.trajectory.length}
          strokeWidth=".025"
        />
      ))}
      {mission.path.length > 1 && (
        <polyline
          points={mission.path.map((p) => p.join(",")).join(" ")}
          fill="none"
          stroke="#8de3d3"
          strokeWidth=".04"
          strokeDasharray=".12 .08"
        />
      )}
      {last?.old_position && last.new_position && (
        <line
          x1={last.old_position[0]}
          y1={last.old_position[2]}
          x2={last.new_position[0]}
          y2={last.new_position[2]}
          stroke="#e9b373"
          strokeWidth=".04"
          strokeDasharray=".08 .07"
        />
      )}
      {mission.objects.map((o) => (
        <g
          key={o.id}
          role="button"
          tabIndex={0}
          aria-label={`Select ${objectName(o)} on map`}
          onKeyDown={(e) => {
            if (e.key === "Enter") onSelect(o.id);
          }}
          onClick={(e) => {
            e.stopPropagation();
            onSelect(o.id);
          }}
          className="map-object"
        >
          <circle
            cx={o.position[0]}
            cy={o.position[2]}
            r={o.id === selected ? 0.15 : 0.09}
            fill={o.state === "moved" ? "#e9b373" : "#81dfd0"}
            stroke="#0e191f"
            strokeWidth=".04"
          />
          <text
            x={o.position[0] + 0.2}
            y={o.position[2] + 0.06}
            fill="#d0e1e4"
            fontSize=".17"
          >
            {objectName(o)}
          </text>
        </g>
      ))}
      {mission.pose && (
        <g
          transform={`translate(${mission.pose.position[0]} ${mission.pose.position[2]}) rotate(${(-mission.pose.yaw_rad * 180) / Math.PI})`}
        >
          <path
            d={`M 0 0 ${scopeArc()
              .map(([x, , z]) => `L ${x} ${z}`)
              .join(" ")} Z`}
            fill="#7cebd0"
            fillOpacity=".08"
            stroke="#7cebd0"
            strokeOpacity=".35"
            strokeWidth=".015"
          />
          <text
            x="0"
            y={LIDAR_RANGE_M + 0.23}
            textAnchor="middle"
            fill="#77aaa1"
            fontSize=".09"
            letterSpacing=".015"
          >
            LiDAR · 5 m MAX
          </text>
          <circle
            r=".23"
            fill="#7cebd0"
            fillOpacity=".12"
            stroke="#7cebd0"
            strokeWidth=".025"
          />
          <path d="M 0 .18 L -.09 -.09 L .09 -.09 Z" fill="#b3ffed" />
        </g>
      )}
    </svg>
  );
}
class RenderBoundary extends Component<
  { children: ReactNode; fallback: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
export default function Scene(props: SceneProps) {
  const [view, setView] = useState<"3d" | "2d">("3d"),
    [reset, setReset] = useState(0),
    [frame, setFrame] = useState(0),
    [layerMenu, setLayerMenu] = useState(false),
    [help, setHelp] = useState(false);
  const [layers, setLayers] = useState<Layers>({
    surfaces: true,
    points: true,
    objects: true,
    trajectory: true,
    occupancy: false,
  });
  const bounds = useMemo(() => {
    const box = new THREE.Box3();
    const point = new THREE.Vector3();
    const arrays = [
      ...props.mission.chunks.map((chunk) => chunk.positions),
      ...props.surfaces.map((patch) => patch.positions),
      ...(props.persistentSurface ? [props.persistentSurface.positions] : []),
    ];
    for (const positions of arrays)
      for (let i = 0; i < positions.length; i += 3)
        box.expandByPoint(
          point.set(positions[i], positions[i + 1], positions[i + 2]),
        );
    return box.isEmpty() ? null : box.getBoundingSphere(new THREE.Sphere());
  }, [props.mission.chunks, props.surfaces, props.persistentSurface]);
  const liveCount = props.mission.chunks.reduce(
    (n, c) => n + c.positions.length / 3,
    0,
  );
  const triangleCount =
    (props.persistentSurface?.indices.length ?? 0) / 3 +
    props.surfaces.reduce((n, p) => n + p.indices.length / 3, 0);
  const container = useRef<HTMLDivElement>(null);
  const labelElements = useRef(new Map<string, HTMLDivElement>());
  const labels = useSceneLabels(
    props.mission,
    props.selected,
    props.onSelect,
    layers.objects,
  );
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (
        (e.key === "Home" || e.key.toLowerCase() === "f") &&
        !e.altKey &&
        !e.ctrlKey &&
        !e.metaKey &&
        !(e.target instanceof HTMLInputElement) &&
        !(e.target instanceof HTMLTextAreaElement) &&
        !(
          e.target instanceof HTMLElement &&
          e.target.closest("select, [contenteditable], dialog")
        )
      ) {
        e.preventDefault();
        if (e.key === "Home") setReset((x) => x + 1);
        else setFrame((x) => x + 1);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
  const count = props.persistentSurface
    ? props.persistentSurface.positions.length / 3
    : props.mission.chunks.reduce((n, c) => n + c.positions.length / 3, 0);
  return (
    <section className="scene-panel" ref={container} aria-label="Spatial view">
      <div
        className="scene-toolbar"
        aria-label="View mode"
        title="Workspace: Escape, right-click or long-press the canvas"
      >
        <div className="segmented">
          <button
            aria-pressed={view === "3d"}
            className={view === "3d" ? "active" : ""}
            onClick={() => setView("3d")}
          >
            3D
          </button>
          <button
            aria-pressed={view === "2d"}
            className={view === "2d" ? "active" : ""}
            onClick={() => setView("2d")}
          >
            2D
          </button>
        </div>
        <button
          aria-label="Frame scan"
          title="Frame scan · F"
          disabled={view !== "3d" || !bounds}
          onClick={() => setFrame((x) => x + 1)}
        >
          <Maximize size={14} />{" "}
          <span className="frame-scan-label">Frame scan</span>
        </button>
      </div>
      <div className="viewport-status" data-testid="viewport-status">
        <span>{props.feedLabel}</span>
        <span>
          {liveCount.toLocaleString()} live points ·{" "}
          {triangleCount.toLocaleString()} surface triangles ·{" "}
          {layers.points ? "points visible" : "points hidden"} ·{" "}
          {layers.surfaces ? "surfaces visible" : "surfaces hidden"}
        </span>
        <span>{props.surfaceReason}</span>
      </div>
      <div className="scene-canvas">
        {view === "3d" ? (
          <RenderBoundary
            fallback={
              <>
                <Map2D {...props} />
                <div className="renderer-note">
                  3D unavailable · showing the 2D map
                </div>
              </>
            }
          >
            <Canvas
              frameloop="demand"
              camera={{
                position: [7.3, 6.5, 8.2],
                fov: 42,
                near: 0.1,
                far: 100,
              }}
              dpr={[1, 1.8]}
              gl={{ antialias: true, alpha: true }}
              onCreated={({ gl }) => {
                gl.setClearColor("#0c141c", 1);
              }}
            >
              <fog attach="fog" args={["#0c141c", 18, 38]} />
              <Suspense fallback={null}>
                <World {...props} layers={layers} />
              </Suspense>
              <Controls
                reset={reset}
                frame={frame}
                bounds={bounds}
                map={props.mission.mapKey}
              />
              <ProjectLabels labels={labels} elements={labelElements} />
            </Canvas>
          </RenderBoundary>
        ) : (
          <Map2D {...props} />
        )}
        {view === "3d" && (
          <div className="scene-label-layer">
            {labels.map((label) => (
              <div
                className="projected-label"
                key={label.id}
                ref={(el) => {
                  if (el) labelElements.current.set(label.id, el);
                  else labelElements.current.delete(label.id);
                }}
              >
                {label.content}
              </div>
            ))}
          </div>
        )}
        {!props.mission.pose &&
          !count &&
          !props.persistentSurface &&
          !props.surfaces.length && (
            <div className="scene-empty">
              <ScanLine size={35} />
              <h3>Waiting for a view of the world</h3>
              <p>
                Scene data appears when the connected source starts streaming.
              </p>
            </div>
          )}
      </div>
      {props.toolsHost &&
        createPortal(
          <div className="scene-tools-panel">
            <div className="scene-caption">
              <span className="live-dot" /> CURRENT SESSION
              <small>ARKit world coordinates · meters</small>
              {props.mission.pose && (
                <span className="pose-readout">
                  CAMERA <b>X {props.mission.pose.position[0].toFixed(2)}</b>
                  <b>Z {props.mission.pose.position[2].toFixed(2)}</b>
                  <em>m</em>
                </span>
              )}
            </div>
            {view === "3d" && layers.surfaces && (
              <div
                className="surface-status"
                data-testid="surface-status"
                data-map-triangles={
                  props.persistentSurface
                    ? props.persistentSurface.indices.length / 3
                    : 0
                }
              >
                <span className="tiny-dot" />
                {props.surfaceStatus === "capacity"
                  ? "Map capacity reached · prior scan retained · export before reset"
                  : props.persistentSurface || props.surfaces.length
                    ? `Coarse preview · ${(props.persistentSurface ? props.persistentSurface.indices.length / 3 : props.surfaces.reduce((n, p) => n + p.indices.length / 3, 0)).toLocaleString()} color triangles${props.persistentSurface ? (props.mapCellM > 0 ? ` · retained grid ${(props.mapCellM * 100).toFixed(0)} cm` : " · before grid coarsening") : ""}${props.surfaceStatus !== "receiving" ? " · capture paused" : ""}`
                    : props.surfaceReason}
              </div>
            )}
            <div className="scene-stat">
              <span>
                {props.persistentSurface ? "MAP VERTICES" : "POINTS RECEIVED"}
              </span>
              <strong>{count.toLocaleString()}</strong>
              <small>
                <span className="tiny-dot" /> {props.mission.objects.length}{" "}
                objects recognized
              </small>
            </div>
            <div className="scene-settings-actions">
              <button
                title="Scene layers"
                aria-label="Scene layers"
                disabled={view === "2d"}
                className={`icon-button ${layerMenu ? "active" : ""}`}
                onClick={() => setLayerMenu(!layerMenu)}
              >
                <Layers3 size={16} />
              </button>
              <button
                title="Reset view · Home"
                aria-label="Reset view"
                disabled={view === "2d"}
                className="icon-button"
                onClick={() => setReset((x) => x + 1)}
              >
                <Maximize size={16} />
              </button>
            </div>
            <div className="scene-bottom">
              <div className="scene-toolbox">
                <button
                  aria-label="View controls help"
                  disabled={view === "2d"}
                  onClick={() => setHelp(!help)}
                >
                  <HelpCircle size={17} />
                </button>
              </div>
              <span className="scene-hint">
                {view === "2d" ? (
                  props.canGoal ? (
                    "Click the map to set a destination"
                  ) : (
                    "X–Z plane · world meters"
                  )
                ) : (
                  <>
                    <MousePointer2 size={12} /> Left-drag pans · right-drag
                    rotates
                  </>
                )}
              </span>
              <div className="axis-widget">
                <span className="axis-y">Y</span>
                <span className="axis-z">Z</span>
                <span className="axis-x">X</span>
                <Crosshair size={22} />
              </div>
            </div>
            {view === "3d" && layerMenu && (
              <div className="scene-popover layers-popover">
                <h4>Scene layers</h4>
                {(Object.keys(layers) as (keyof Layers)[]).map((key) => (
                  <label key={key}>
                    <input
                      type="checkbox"
                      checked={layers[key]}
                      onChange={() =>
                        setLayers({ ...layers, [key]: !layers[key] })
                      }
                    />
                    {key === "surfaces"
                      ? "Color surfaces"
                      : key === "points"
                        ? "Point cloud"
                        : key === "objects"
                          ? "Object labels"
                          : key === "trajectory"
                            ? "Rover trail"
                            : "Occupancy grid"}
                  </label>
                ))}
              </div>
            )}
            {view === "3d" && help && (
              <div className="scene-popover help-popover">
                <h4>Find your perspective</h4>
                <p>
                  <kbd>Left drag</kbd> Pan
                </p>
                <p>
                  <kbd>Right drag</kbd> Rotate
                </p>
                <p>
                  <kbd>Middle drag</kbd> Orbit
                </p>
                <p>
                  <kbd>Shift + middle</kbd> Pan
                </p>
                <p>
                  <kbd>Scroll</kbd> Zoom
                </p>
                <p>
                  <kbd>Home</kbd> Reset view
                </p>
                <small>
                  Click without dragging to navigate. Right-click without
                  dragging opens the workspace. Two-finger touch pans and zooms.
                </small>
              </div>
            )}
            <div className="scene-footer">
              <span>
                <i className="legend-point" />{" "}
                {view === "2d"
                  ? "Occupancy"
                  : layers.surfaces
                    ? "Color surfaces"
                    : "Point cloud"}
              </span>
              <span>
                <i className="legend-rover" /> Rover / phone
              </span>
              <span>
                <i className="legend-change" /> Change detected
              </span>
              <span
                className="scene-unit"
                title="Apple documents a 5 m LiDAR depth limit. Viewing angle awaits camera intrinsics; this is not observed coverage."
              >
                5 m LiDAR · FOV UNCALIBRATED
              </span>
            </div>
          </div>,
          props.toolsHost,
        )}
    </section>
  );
}
