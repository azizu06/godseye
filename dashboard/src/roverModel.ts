import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";

/**
 * Display-only footprint of the rover glyph (metres, local frame: +X right,
 * +Y up, +Z forward). This is the long-standing placeholder box size, not a
 * measured rover calibration; motion readiness uses GODSEYE_ROVER_CALIBRATION.
 */
export const ROVER_FOOTPRINT = { width: 0.25, height: 0.12, length: 0.35 };

export type RoverMaterial =
  | "deck"
  | "tire"
  | "yellow"
  | "brass"
  | "pcb"
  | "board"
  | "orange"
  | "silver"
  | "servo"
  | "phone"
  | "trim"
  | "beaconRed"
  | "beaconBlue";

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
) {
  (parts[material] ??= []).push(
    new THREE.BoxGeometry(...size).translate(...at),
  );
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
}

/** ELEGOO-style acrylic deck: flat rear, rounded nose. */
function deck(parts: Parts, halfWidth: number, bottom: number) {
  const rear = -0.165;
  const nose = 0.168;
  const shoulder = 0.09;
  const shape = new THREE.Shape()
    .moveTo(-halfWidth, rear)
    .lineTo(halfWidth, rear)
    .lineTo(halfWidth, shoulder)
    .quadraticCurveTo(halfWidth, nose, 0, nose)
    .quadraticCurveTo(-halfWidth, nose, -halfWidth, shoulder)
    .closePath();
  const g = new THREE.ExtrudeGeometry(shape, {
    depth: 0.004,
    bevelEnabled: false,
    curveSegments: 6,
  });
  // Shape Y becomes +Z; extrusion runs down from `bottom + depth`.
  g.rotateX(Math.PI / 2).translate(0, bottom + 0.004, 0);
  (parts.deck ??= []).push(g);
}

/**
 * Low-poly responder rover modelled on the team's ELEGOO Smart Robot Car V4:
 * twin black decks on brass standoffs, four knobby tyres with yellow rims and
 * yellow TT motors, a board stack, the rear battery pack (rescue orange here),
 * the front servo with its twin ultrasonic eyes, and the mounted phone.
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
      box(
        parts,
        "yellow",
        [0.05, 0.022, 0.024],
        [sx * 0.06, AXLE_Y - 0.008, z],
      );
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
  // Controller stack.
  box(parts, "pcb", [0.07, 0.004, 0.08], [0, top + 0.002, -0.005]);
  box(parts, "board", [0.064, 0.012, 0.07], [0, top + 0.01, -0.005]);
  box(parts, "silver", [0.03, 0.006, 0.008], [0.01, top + 0.019, 0.02]);
  // Battery pack and light bar.
  box(parts, "orange", [0.11, 0.034, 0.065], [0, top + 0.017, -0.125]);
  box(parts, "beaconRed", [0.022, 0.01, 0.012], [-0.012, top + 0.039, -0.1]);
  box(parts, "beaconBlue", [0.022, 0.01, 0.012], [0.012, top + 0.039, -0.1]);
  // Front servo, ultrasonic board and its two eyes.
  box(parts, "servo", [0.024, 0.02, 0.022], [0, top + 0.01, 0.135]);
  box(parts, "board", [0.07, 0.028, 0.004], [0, top + 0.032, 0.15]);
  for (const x of [-0.02, 0.02])
    cylinder(parts, "silver", 0.0095, 0.012, 10, "z", [x, top + 0.032, 0.158]);
  // Mounted phone: landscape, cameras forward, on a small cradle.
  box(parts, "orange", [0.05, 0.006, 0.02], [0, top + 0.003, 0.075]);
  box(parts, "phone", [0.14, 0.068, 0.008], [0, top + 0.04, 0.075]);
  box(parts, "trim", [0.03, 0.03, 0.004], [0.045, top + 0.056, 0.081]);

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

/** Top-of-battery marking position (local frame). */
export const SCOUT_DECAL = {
  position: [0, 0.024 + 0.034 + 0.0005, -0.135] as const,
  size: [0.085, 0.026] as const,
};
