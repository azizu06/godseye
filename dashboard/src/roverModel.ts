import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";

/**
 * Display-only footprint of the rover glyph (metres, local frame: +Y up,
 * +Z forward). This is the long-standing placeholder box size, not a
 * measured rover calibration; motion readiness uses GODSEYE_ROVER_CALIBRATION.
 */
export const ROVER_FOOTPRINT = { width: 0.25, height: 0.12, length: 0.35 };

export type RoverMaterial =
  | "deck"
  | "tire"
  | "yellow"
  | "brass"
  | "ivory"
  | "orange"
  | "black"
  | "silver"
  | "servo"
  | "trim";

type Parts = Partial<Record<RoverMaterial, THREE.BufferGeometry[]>>;

const FLOOR = -ROVER_FOOTPRINT.height / 2;
const WHEEL_R = 0.042;
const WHEEL_W = 0.03;
const WHEEL_X = 0.103;
const WHEEL_Z = 0.1;
const AXLE_Y = FLOOR + WHEEL_R;

function box(
  parts: Parts,
  material: RoverMaterial,
  size: [number, number, number],
  at: [number, number, number],
  yaw = 0,
) {
  const g = new THREE.BoxGeometry(...size).rotateY(yaw).translate(...at);
  (parts[material] ??= []).push(g);
  return g;
}

/** Cylinder along X (wheels), Y (standoffs) or Z (sensor eyes). */
function cylinder(
  parts: Parts,
  material: RoverMaterial,
  radius: number,
  length: number,
  segments: number,
  axis: "x" | "y" | "z",
  at: [number, number, number],
) {
  const g = new THREE.CylinderGeometry(radius, radius, length, segments);
  if (axis === "x") g.rotateZ(Math.PI / 2);
  else if (axis === "z") g.rotateX(Math.PI / 2);
  (parts[material] ??= []).push(g.translate(...at));
  return g;
}

/** ELEGOO-style acrylic deck: flat rear, rounded nose. */
function deck(parts: Parts, halfWidth: number, bottom: number) {
  const rear = -0.165;
  const nose = 0.168;
  const shoulder = 0.09;
  const y = bottom + 0.002;
  box(
    parts,
    "deck",
    [halfWidth * 2, 0.004, shoulder - rear],
    [0, y, (rear + shoulder) / 2],
  );
  (parts.deck ??= []).push(
    new THREE.CylinderGeometry(
      halfWidth,
      halfWidth,
      0.004,
      12,
      1,
      false,
      -Math.PI / 2,
      Math.PI,
    )
      .scale(1, 1, (nose - shoulder) / halfWidth)
      .translate(0, y, shoulder),
  );
}

/**
 * Low-poly SCOUT responder rover. The chassis follows the team's ELEGOO Smart
 * Robot Car V4 photos: twin black decks on brass standoffs, four tyres with
 * yellow rims, TT motors, the rear battery box and the front servo with twin
 * ultrasonic eyes. The ivory cover, orange bumper/accents and upright phone
 * slab follow the generated SCOUT concept and are styling, not hardware.
 * Geometry is merged per material so the whole model is one draw per colour.
 */
export function buildResponderRover(): Map<
  RoverMaterial,
  THREE.BufferGeometry
> {
  const parts: Parts = {};
  for (const sx of [-1, 1])
    for (const sz of [-1, 1]) {
      const x = sx * WHEEL_X;
      const z = sz * WHEEL_Z;
      cylinder(parts, "tire", WHEEL_R, WHEEL_W, 12, "x", [x, AXLE_Y, z]);
      // Five-sided rim nods to the five-spoke yellow wheels.
      cylinder(parts, "yellow", 0.027, 0.004, 5, "x", [
        sx * (WHEEL_X + WHEEL_W / 2 + 0.002),
        AXLE_Y,
        z,
      ]);
      cylinder(parts, "trim", 0.008, 0.002, 8, "x", [
        sx * (WHEEL_X + WHEEL_W / 2 + 0.004),
        AXLE_Y,
        z,
      ]);
      box(parts, "ivory", [0.05, 0.022, 0.024], [sx * 0.06, AXLE_Y - 0.008, z]);
    }
  deck(parts, 0.078, -0.022);
  deck(parts, 0.085, 0.02);
  for (const [x, z] of [
    [-0.065, -0.13],
    [0.065, -0.13],
    [-0.065, 0.02],
    [0.065, 0.02],
    [-0.04, 0.13],
    [0.04, 0.13],
  ])
    cylinder(parts, "brass", 0.0035, 0.038, 6, "y", [x, 0.001, z]);
  const top = 0.024;
  // Rear battery box and the protective cover over the controller stack.
  box(parts, "black", [0.1, 0.032, 0.06], [0, top + 0.016, -0.13]);
  box(parts, "ivory", [0.084, 0.036, 0.1], [0, top + 0.018, -0.035]);
  for (const sx of [-1, 1])
    box(
      parts,
      "orange",
      [0.003, 0.026, 0.022],
      [sx * 0.0435, top + 0.018, 0.004],
    );
  // Front: wrap-around bumper (its angled wings also point the way), servo,
  // ultrasonic bracket and the two eyes with dark transducer faces.
  box(parts, "orange", [0.06, 0.018, 0.012], [0, top - 0.004, 0.163]);
  for (const sx of [-1, 1])
    box(
      parts,
      "orange",
      [0.032, 0.018, 0.012],
      [sx * 0.042, top - 0.004, 0.152],
      sx * 0.5,
    );
  box(parts, "servo", [0.024, 0.02, 0.022], [0, top + 0.01, 0.14]);
  box(parts, "black", [0.066, 0.026, 0.004], [0, top + 0.033, 0.153]);
  for (const x of [-0.02, 0.02]) {
    cylinder(parts, "silver", 0.0095, 0.012, 10, "z", [x, top + 0.033, 0.161]);
    cylinder(parts, "trim", 0.007, 0.002, 10, "z", [x, top + 0.033, 0.1675]);
  }
  // Upright phone/sensor slab behind the eyes, leaning slightly back, with
  // its camera pair facing forward. A proposed mount, not observed hardware.
  box(parts, "black", [0.012, 0.025, 0.008], [0, top + 0.033, 0.143]);
  const mount = new THREE.Matrix4()
    .makeTranslation(0, top + 0.045, 0.145)
    .multiply(new THREE.Matrix4().makeRotationX(-0.2));
  box(parts, "black", [0.05, 0.09, 0.006], [0, 0.045, 0]).applyMatrix4(mount);
  for (const x of [-0.012, 0.012])
    cylinder(parts, "trim", 0.005, 0.002, 8, "z", [
      x,
      0.075,
      0.004,
    ]).applyMatrix4(mount);

  const merged = new Map<RoverMaterial, THREE.BufferGeometry>();
  for (const [key, list] of Object.entries(parts) as [
    RoverMaterial,
    THREE.BufferGeometry[],
  ][]) {
    const flat = list.map((g) => {
      const f = g.index ? g.toNonIndexed() : g;
      f.clearGroups();
      if (f !== g) g.dispose();
      return f;
    });
    const geometry = mergeGeometries(flat);
    for (const g of flat) g.dispose();
    geometry.computeBoundingBox();
    merged.set(key, geometry);
  }
  return merged;
}

/** SCOUT markings on the cover: top (read from behind) and both sides. */
export const SCOUT_DECALS = [
  {
    position: [0, 0.0605, -0.035],
    rotation: [-Math.PI / 2, 0, Math.PI],
    size: [0.07, 0.021],
  },
  {
    position: [0.0425, 0.042, -0.042],
    rotation: [0, Math.PI / 2, 0],
    size: [0.06, 0.018],
  },
  {
    position: [-0.0425, 0.042, -0.042],
    rotation: [0, -Math.PI / 2, 0],
    size: [0.06, 0.018],
  },
] as const;
