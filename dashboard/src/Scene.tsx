import PointCloudLayer from "./PointCloudLayer";
import type { PointCloudStore } from "./pointCloud";
import { createPortal } from "react-dom";
import { ColorSurfaces } from "./ColorSurfaces";
import type { SurfacePatch, SurfaceStatus } from "./surfaceTypes";
import {
  Component,
  Suspense,
  type RefObject,
  useEffect,
  useSyncExternalStore,
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
import { decodeCells, type Vec3, type WorldObject } from "./protocol";
import {
  ProjectLabels,
  useSceneLabels,
  type ApproachDrawing,
} from "./SceneLabels";
import { displayedObjects, objectEvidence } from "./objectDisplay";
import type { LiveMarker } from "./detections";
import { displayedStoredObjects, type PersonTrack } from "./personMemory";
import { LIDAR_RANGE_M, scopeArc, scopeTriangles } from "./sensorProfile";
import { displayFloorY, overlayY } from "./floor";

export const objectName = (o: WorldObject) =>
  o.class === "potted plant"
    ? "Plant"
    : o.class.charAt(0).toUpperCase() + o.class.slice(1);
export interface SceneProps {
  cloud: PointCloudStore;
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
  /** Fresh detections placed by their own frame's depth; empty when stale. */
  liveDetections: LiveMarker[];
  /** Last measured people not currently LIVE; display only, never navigation input. */
  retainedPeople: PersonTrack[];
  /** Suggested walking route; visualization only, never a rover goal. */
  approachRoute: ApproachDrawing | null;
  /** Viewer clock (ms) for object last-seen wording. */
  now: number;
  pickingRouteStart: boolean;
  onRouteStart: (x: number, z: number) => void;
  view: "3d" | "2d";
  onView: (view: "3d" | "2d") => void;
  /** Viewer display state (voice actions); layer checkboxes still apply. */
  showBoxes: boolean;
  showLabels: boolean;
  handle?: RefObject<SceneHandle>;
}
export interface CameraPose {
  position: Vec3;
  target: Vec3;
}
/** Imperative view hooks for dashboard actions; visualization only. */
export interface SceneHandle {
  /** Present while the 3D view is mounted. */
  camera?: {
    get(): CameraPose;
    set(pose: CameraPose): void;
    focus(position: Vec3): void;
    frame(): void;
  };
  capture?: () => Promise<Blob | null>;
  /** Applied when the 3D view next mounts, after a switch from 2D. */
  pending?: { pose?: CameraPose; frame?: boolean; focus?: Vec3 };
}
interface Layers {
  surfaces: boolean;
  points: boolean;
  objects: boolean;
  trajectory: boolean;
  occupancy: boolean;
  weakObjects: boolean;
}

function Controls({
  reset,
  frame,
  bounds,
  map,
  handle,
}: {
  reset: number;
  frame: number;
  bounds: THREE.Sphere | null;
  map: string | null;
  handle?: RefObject<SceneHandle>;
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
  useEffect(() => {
    const view = handle?.current;
    if (!view) return;
    const move = (position: THREE.Vector3, target: THREE.Vector3) => {
      const controls = ref.current;
      if (!controls) return;
      controls.enableDamping = false;
      controls.update();
      controls.target.copy(target);
      camera.position.copy(position);
      camera.updateProjectionMatrix();
      controls.update();
      controls.enableDamping = true;
      invalidate();
    };
    const api: NonNullable<SceneHandle["camera"]> = {
      get: () => ({
        position: camera.position.toArray() as Vec3,
        target: (ref.current?.target.toArray() ?? [0, 0, 0]) as Vec3,
      }),
      set: (pose) =>
        move(
          new THREE.Vector3(...pose.position),
          new THREE.Vector3(...pose.target),
        ),
      focus: (p) => {
        const controls = ref.current;
        if (!controls) return;
        const target = new THREE.Vector3(p[0], p[1] + 0.3, p[2]);
        const direction = camera.position
          .clone()
          .sub(controls.target)
          .normalize();
        move(target.clone().addScaledVector(direction, 2.6), target);
      },
      frame: () => latestFit.current(),
    };
    view.camera = api;
    const pending = view.pending;
    view.pending = undefined;
    if (pending?.pose) api.set(pending.pose);
    if (pending?.frame) api.frame();
    if (pending?.focus) api.focus(pending.focus);
    return () => {
      if (view.camera === api) view.camera = undefined;
    };
  }, [handle, camera, invalidate]);
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
function OccupancyMesh({
  mission,
  floor,
}: {
  mission: Mission;
  floor: number | null;
}) {
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
        overlayY(floor, "occupancy"),
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
function ViewScope({
  mission,
  floor,
}: {
  mission: Mission;
  floor: number | null;
}) {
  const pose = mission.pose;
  const fan = useMemo(() => scopeTriangles(), []);
  const arc = useMemo(() => scopeArc(), []);
  if (!pose) return null;
  return (
    <group
      position={[pose.position[0], overlayY(floor, "scope"), pose.position[2]]}
      rotation={[0, pose.yaw_rad, 0]}
    >
      <mesh>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" args={[fan, 3]} />
        </bufferGeometry>
        <meshBasicMaterial
          color="#8ab4f8"
          transparent
          opacity={0.075}
          side={THREE.DoubleSide}
          depthWrite={false}
        />
      </mesh>
      <Line
        points={[[0, 0, 0], arc[0]]}
        color="#91b4e6"
        lineWidth={0.8}
        transparent
        opacity={0.4}
      />
      <Line
        points={[[0, 0, 0], arc[arc.length - 1]]}
        color="#91b4e6"
        lineWidth={0.8}
        transparent
        opacity={0.4}
      />
      {[1, 3, LIDAR_RANGE_M].map((radius) => (
        <Line
          key={radius}
          points={scopeArc(radius)}
          color="#91b4e6"
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
        color="#a6c2eb"
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
function Trajectory({
  mission,
  floor,
}: {
  mission: Mission;
  floor: number | null;
}) {
  const colors = useMemo(
    () =>
      mission.trajectory.map((_, i) =>
        new THREE.Color("#303946").lerp(
          new THREE.Color("#8faed6"),
          i / Math.max(1, mission.trajectory.length - 1),
        ),
      ),
    [mission.trajectory],
  );
  if (mission.trajectory.length < 2) return null;
  return (
    <Line
      points={mission.trajectory.map((p) => [
        p[0],
        overlayY(floor, "trajectory"),
        p[2],
      ])}
      vertexColors={colors}
      lineWidth={1.5}
      transparent
      opacity={0.65}
    />
  );
}
function CaptureBridge({ handle }: { handle?: RefObject<SceneHandle> }) {
  const { gl, scene, camera } = useThree();
  useEffect(() => {
    const view = handle?.current;
    if (!view) return;
    // Render and read in one task, so the drawing buffer still holds this frame.
    const capture = () =>
      new Promise<Blob | null>((resolve) => {
        gl.render(scene, camera);
        gl.domElement.toBlob(resolve, "image/png");
      });
    view.capture = capture;
    return () => {
      if (view.capture === capture) view.capture = undefined;
    };
  }, [gl, scene, camera, handle]);
  return null;
}
function World({
  cloud,
  persistentSurface,
  surfaces,
  mission,
  selected,
  layers,
  canGoal,
  onGoal,
  liveDetections,
  retainedPeople,
  approachRoute,
  pickingRouteStart,
  onRouteStart,
}: {
  cloud: PointCloudStore;
  persistentSurface: SurfacePatch | null;
  surfaces: SurfacePatch[];
  mission: Mission;
  selected: string | null;
  layers: Layers;
  canGoal: boolean;
  onGoal: (x: number, z: number) => void;
  liveDetections: LiveMarker[];
  retainedPeople: PersonTrack[];
  approachRoute: ApproachDrawing | null;
  /** Viewer clock (ms) for object last-seen wording. */
  now: number;
  pickingRouteStart: boolean;
  onRouteStart: (x: number, z: number) => void;
}) {
  const selectedEvent = mission.events
    .filter(
      (e) =>
        (e.object_id === selected || e.new_object_id === selected) &&
        (e.kind === "moved" || e.kind === "possible_move"),
    )
    .at(-1);
  // Floor overlays share the confirmed map floor; measured geometry keeps world Y.
  const floor = displayFloorY(mission.occupancy);
  const pick = overlayY(floor, "pick"),
    approachY = overlayY(floor, "approach");
  // People linked to a retained marker are drawn once, by that marker.
  const storedObjects = displayedStoredObjects(
    mission.objects,
    mission.people,
    selected,
  );
  return (
    <>
      {canGoal && (
        <mesh
          name="goal-pick-plane"
          rotation={[-Math.PI / 2, 0, 0]}
          position={[0, pick, 0]}
          onClick={(e) => {
            if (e.button === 0 && e.delta < 4) onGoal(e.point.x, e.point.z);
          }}
        >
          <planeGeometry args={[40, 40]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
      )}
      {pickingRouteStart && (
        <mesh
          name="route-start-pick-plane"
          rotation={[-Math.PI / 2, 0, 0]}
          position={[0, pick, 0]}
          onClick={(e) => {
            if (e.button === 0 && e.delta < 4)
              onRouteStart(e.point.x, e.point.z);
          }}
        >
          <planeGeometry args={[40, 40]} />
          <meshBasicMaterial transparent opacity={0} depthWrite={false} />
        </mesh>
      )}
      {approachRoute && (
        <group name="approach-route">
          <mesh
            position={[
              approachRoute.start[0],
              approachY,
              approachRoute.start[1],
            ]}
            rotation={[-Math.PI / 2, 0, 0]}
          >
            <ringGeometry args={[0.12, 0.16, 32]} />
            <meshBasicMaterial color="#ffb86b" side={THREE.DoubleSide} />
          </mesh>
          {approachRoute.points && (
            <Line
              points={approachRoute.points.map((p) => [p[0], approachY, p[1]])}
              color="#ffb86b"
              lineWidth={3}
            />
          )}
          {approachRoute.approach && (
            <mesh
              position={[
                approachRoute.approach[0],
                approachY,
                approachRoute.approach[1],
              ]}
              rotation={[-Math.PI / 2, 0, 0]}
            >
              <circleGeometry args={[0.12, 32]} />
              <meshBasicMaterial color="#ffb86b" side={THREE.DoubleSide} />
            </mesh>
          )}
        </group>
      )}
      <ambientLight intensity={1.5} />
      <directionalLight position={[4, 8, 3]} intensity={2} />
      <Grid
        name="floor-grid"
        position={[0, overlayY(floor, "grid"), 0]}
        args={[26, 26]}
        cellSize={0.5}
        cellThickness={0.5}
        cellColor="#34373c"
        sectionSize={2}
        sectionThickness={0.8}
        sectionColor="#51565e"
        fadeDistance={25}
        fadeStrength={1.6}
        infiniteGrid
      />
      {layers.surfaces && persistentSurface && (
        <ColorSurfaces patches={[persistentSurface]} retained />
      )}
      {layers.surfaces && <ColorSurfaces patches={surfaces} />}
      <PointCloudLayer
        cloud={cloud}
        visible={layers.points}
        surfaceOcclusion={layers.surfaces}
      />
      {layers.occupancy && <OccupancyMesh mission={mission} floor={floor} />}
      {layers.trajectory && <Trajectory mission={mission} floor={floor} />}
      {mission.path.length > 1 && (
        <Line
          points={mission.path.map((p) => [
            p[0],
            overlayY(floor, "path"),
            p[1],
          ])}
          color="#98b9ea"
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
        displayedObjects(storedObjects, layers.weakObjects, selected).map(
          (o) => (
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
                  color={o.id === selected ? "#c2d6f5" : "#718198"}
                  transparent
                  opacity={o.id === selected ? 0.28 : 0.12}
                />
                <Edges
                  color={
                    o.state === "moved"
                      ? "#e9b373"
                      : o.id === selected
                        ? "#d1e1fa"
                        : "#97aecb"
                  }
                />
              </mesh>
              <Line
                points={[
                  [0, 0.1, 0],
                  [0, 0.72, 0],
                ]}
                color={o.state === "moved" ? "#e9b373" : "#91a6c2"}
                transparent
                opacity={0.6}
              />
            </group>
          ),
        )}
      {retainedPeople.map((person) => (
        <group
          key={person.id}
          name="retained-person-marker"
          position={person.position}
        >
          <mesh>
            <sphereGeometry args={[0.07, 16, 12]} />
            <meshBasicMaterial color="#ff7a66" transparent opacity={0.45} />
          </mesh>
          <mesh>
            <sphereGeometry args={[0.14, 12, 8]} />
            <meshBasicMaterial
              color="#ff7a66"
              wireframe
              transparent
              opacity={0.3}
              depthWrite={false}
            />
          </mesh>
          {floor !== null && person.position[1] > floor && (
            <Line
              points={[
                [0, 0, 0],
                [0, floor - person.position[1], 0],
              ]}
              color="#ff7a66"
              dashed
              dashSize={0.06}
              gapSize={0.05}
              transparent
              opacity={0.45}
            />
          )}
        </group>
      ))}
      {liveDetections.map((d) => (
        <group key={d.key} name="live-detection-marker" position={d.position}>
          <mesh>
            <sphereGeometry args={[0.07, 16, 12]} />
            <meshBasicMaterial
              color={d.class === "person" ? "#ff7a66" : "#f2d27a"}
            />
          </mesh>
          <mesh>
            <sphereGeometry args={[0.14, 20, 14]} />
            <meshBasicMaterial
              color={d.class === "person" ? "#ff7a66" : "#f2d27a"}
              transparent
              opacity={0.22}
              depthWrite={false}
            />
          </mesh>
        </group>
      ))}
      <ViewScope mission={mission} floor={floor} />
      {mission.pose && (
        <group
          name="rover-phone-glyph"
          position={[
            mission.pose.position[0],
            overlayY(floor, "rover"),
            mission.pose.position[2],
          ]}
          rotation={[0, mission.pose.yaw_rad, 0]}
        >
          <mesh rotation={[-Math.PI / 2, 0, 0]}>
            <ringGeometry args={[0.2, 0.23, 48]} />
            <meshBasicMaterial color="#a4c5f2" side={THREE.DoubleSide} />
          </mesh>
          <mesh>
            <boxGeometry args={[0.25, 0.12, 0.35]} />
            <meshStandardMaterial
              color="#a5b7cf"
              metalness={0.6}
              roughness={0.3}
              emissive="#293c58"
            />
            <Edges color="#c6d9f5" />
          </mesh>
          <mesh position={[0, 0.16, 0]} rotation={[-0.15, 0, 0]}>
            <boxGeometry args={[0.12, 0.2, 0.035]} />
            <meshStandardMaterial color="#263953" />
            <Edges color="#bad1f2" />
          </mesh>
          <mesh position={[0, 0.02, 0.33]} rotation={[Math.PI / 2, 0, 0]}>
            <coneGeometry args={[0.1, 0.2, 3]} />
            <meshBasicMaterial color="#d8e7fc" />
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
            stroke="#373c44"
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
                fill={c === 2 ? "#b98d60" : "#3d526f"}
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
          stroke="#8faed6"
          strokeOpacity={0.12 + (0.5 * i) / mission.trajectory.length}
          strokeWidth=".025"
        />
      ))}
      {mission.path.length > 1 && (
        <polyline
          points={mission.path.map((p) => p.join(",")).join(" ")}
          fill="none"
          stroke="#a0bde7"
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
            fill={o.state === "moved" ? "#e9b373" : "#94b7e8"}
            stroke="#1b1e24"
            strokeWidth=".04"
          />
          <text
            x={o.position[0] + 0.2}
            y={o.position[2] + 0.06}
            fill="#d9e0e9"
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
            fill="#a3c4f2"
            fillOpacity=".08"
            stroke="#a3c4f2"
            strokeOpacity=".35"
            strokeWidth=".015"
          />
          <text
            x="0"
            y={LIDAR_RANGE_M + 0.23}
            textAnchor="middle"
            fill="#879fbd"
            fontSize=".09"
            letterSpacing=".015"
          >
            LiDAR · 5 m MAX
          </text>
          <circle
            r=".23"
            fill="#a3c4f2"
            fillOpacity=".12"
            stroke="#a3c4f2"
            strokeWidth=".025"
          />
          <path d="M 0 .18 L -.09 -.09 L .09 -.09 Z" fill="#d8e7fc" />
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
  const { view, onView: setView } = props;
  const [reset, setReset] = useState(0),
    [frame, setFrame] = useState(0),
    [layerMenu, setLayerMenu] = useState(false),
    [help, setHelp] = useState(false);
  const [layers, setLayers] = useState<Layers>({
    surfaces: true,
    points: true,
    objects: true,
    trajectory: true,
    occupancy: false,
    weakObjects: false,
  });
  useSyncExternalStore(props.cloud.subscribe, props.cloud.snapshot);
  const hasGeometry =
    props.cloud.count > 0 ||
    !!props.persistentSurface ||
    props.surfaces.length > 0;
  const bounds = useMemo(() => {
    const box = new THREE.Box3();
    const point = new THREE.Vector3();
    for (let i = 0; i < props.cloud.count * 3; i += 3)
      box.expandByPoint(
        point.set(
          props.cloud.positions[i],
          props.cloud.positions[i + 1],
          props.cloud.positions[i + 2],
        ),
      );
    for (const patch of [
      ...props.surfaces,
      ...(props.persistentSurface ? [props.persistentSurface] : []),
    ]) {
      const seen = new Uint8Array(patch.positions.length / 3);
      for (const index of patch.indices) {
        if (seen[index]) continue;
        seen[index] = 1;
        box.expandByPoint(
          point.set(
            patch.positions[index * 3],
            patch.positions[index * 3 + 1],
            patch.positions[index * 3 + 2],
          ),
        );
      }
    }
    return box.isEmpty() ? null : box.getBoundingSphere(new THREE.Sphere());
  }, [props.mission.mapKey, frame, hasGeometry]);
  const liveCount = props.cloud.count;
  const triangleCount =
    (props.persistentSurface?.indices.length ?? 0) / 3 +
    props.surfaces.reduce((n, p) => n + p.indices.length / 3, 0);
  const container = useRef<HTMLDivElement>(null);
  const labelElements = useRef(new Map<string, HTMLDivElement>());
  const weakCount = props.mission.objects.filter(
    (o) => objectEvidence(o) === "weak",
  ).length;
  const labels = useSceneLabels(
    props.mission,
    props.selected,
    props.onSelect,
    layers.objects && props.showLabels,
    props.showLabels ? props.liveDetections : [],
    props.approachRoute,
    // Ten-second steps keep age wording current without re-rendering the map every tick.
    Math.floor(props.now / 10_000) * 10_000,
    layers.weakObjects,
    displayFloorY(props.mission.occupancy),
    props.showLabels ? props.retainedPeople : [],
  );
  useEffect(() => {
    const handle = props.handle?.current;
    const svg = container.current?.querySelector(".scene-canvas svg");
    if (view !== "2d" || !handle || !svg) return;
    const capture = async () =>
      new Blob([new XMLSerializer().serializeToString(svg)], {
        type: "image/svg+xml",
      });
    handle.capture = capture;
    return () => {
      if (handle.capture === capture) handle.capture = undefined;
    };
  }, [view, props.handle]);
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
    : props.cloud.count;
  const floor = displayFloorY(props.mission.occupancy);
  return (
    <section className="scene-panel" ref={container} aria-label="Spatial view">
      <div
        className="scene-toolbar"
        aria-label="View mode"
        title="Workspace: Escape, right-click or long-press the canvas"
      >
        <div
          className="viewport-brand"
          aria-label="God’s Eye spatial workspace"
        >
          <Crosshair size={19} strokeWidth={1.4} aria-hidden="true" />
          <span>
            GOD’S EYE<small>SPATIAL WORKSPACE</small>
          </span>
        </div>
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
          {(layers.points
            ? layers.surfaces
              ? props.cloud.visibleCount
              : props.cloud.count
            : 0
          ).toLocaleString()}{" "}
          dots drawn ·{" "}
          {layers.surfaces ? "surfaces visible" : "surfaces hidden"}
        </span>
        <span>{props.surfaceReason}</span>
        <span data-testid="floor-status">
          {floor === null
            ? "Floor unknown · overlays at AR origin height"
            : `Floor Y ${floor.toFixed(2)} m · overlays on floor`}
        </span>
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
                gl.setClearColor("#202226", 1);
              }}
            >
              <fog attach="fog" args={["#202226", 18, 38]} />
              <Suspense fallback={null}>
                <World
                  {...props}
                  layers={{
                    ...layers,
                    objects: layers.objects && props.showBoxes,
                  }}
                />
              </Suspense>
              <Controls
                reset={reset}
                frame={frame}
                bounds={bounds}
                map={props.mission.mapKey}
                handle={props.handle}
              />
              <CaptureBridge handle={props.handle} />
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
                    ? `Coarse preview · ${(props.persistentSurface ? props.persistentSurface.indices.length / 3 : props.surfaces.reduce((n, p) => n + p.indices.length / 3, 0)).toLocaleString()} color triangles${props.persistentSurface ? (props.mapCellM > 0 ? ` · adaptive grid up to ${(props.mapCellM * 100).toFixed(0)} cm` : " · before grid coarsening") : ""}${props.surfaceStatus !== "receiving" ? " · capture paused" : ""}`
                    : props.surfaceReason}
              </div>
            )}
            {props.cloud.retirementCapacity && (
              <p className="surface-status" role="status">
                Moving-object cleanup limit reached · some old points remain
                until a new session
              </p>
            )}
            <div className="scene-stat">
              <span>
                {props.persistentSurface ? "MAP VERTICES" : "POINTS RECEIVED"}
              </span>
              <strong>{count.toLocaleString()}</strong>
              <small>
                <span className="tiny-dot" /> {props.mission.objects.length}{" "}
                objects recognized
                {weakCount > 0 && !layers.weakObjects && (
                  <span data-testid="weak-hidden">
                    {" "}
                    · {weakCount} low-evidence hidden in 3D
                  </span>
                )}
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
                            : key === "weakObjects"
                              ? `Low-evidence objects (${weakCount})`
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
              <span title="Illustrative camera-position glyph on the floor; not a calibrated chassis pose">
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
