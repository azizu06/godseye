import { Edges } from "@react-three/drei";
import * as THREE from "three";
import {
  buildResponderRover,
  ROVER_FOOTPRINT,
  SCOUT_DECAL,
  type RoverMaterial,
} from "./roverModel";

const COLORS: Record<RoverMaterial, string> = {
  deck: "#2c323b",
  tire: "#17191d",
  yellow: "#d9b64a",
  brass: "#b8955a",
  pcb: "#2f6b4f",
  board: "#1b1e23",
  orange: "#e07b39",
  silver: "#c9ced6",
  servo: "#3d5a99",
  phone: "#111316",
  trim: "#3a3f47",
  beaconRed: "#d05a4e",
  beaconBlue: "#8ab4f8",
};

let shared:
  | {
      meshes: [THREE.BufferGeometry, THREE.Material][];
      decal: THREE.Material;
    }
  | undefined;

function scoutTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 80;
  const ctx = canvas.getContext("2d");
  if (ctx) {
    ctx.fillStyle = "#1b1e23";
    ctx.font = "700 60px 'IBM Plex Mono', monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("SCOUT", 128, 44);
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 4;
  return texture;
}

/** Built once and reused: the glyph remounts whenever pose drops out. */
function roverAssets() {
  shared ??= {
    meshes: [...buildResponderRover()].map(([key, geometry]) => [
      geometry,
      new THREE.MeshStandardMaterial({
        color: COLORS[key],
        flatShading: true,
        roughness: key === "brass" || key === "silver" ? 0.35 : 0.6,
        metalness: key === "brass" || key === "silver" ? 0.6 : 0.1,
        ...(key.startsWith("beacon")
          ? { emissive: COLORS[key], emissiveIntensity: 0.5 }
          : {}),
      }),
    ]),
    decal: new THREE.MeshBasicMaterial({
      map: scoutTexture(),
      transparent: true,
      depthWrite: false,
    }),
  };
  return shared;
}

/**
 * Responder-styled rover model inside the unchanged footprint outline. Local
 * frame only (+Z forward); the caller owns pose, floor anchoring and heading.
 */
export function ResponderRover() {
  const { meshes, decal } = roverAssets();
  const { width, height, length } = ROVER_FOOTPRINT;
  return (
    <group name="responder-rover">
      <mesh>
        <boxGeometry args={[width, height, length]} />
        <meshBasicMaterial
          color="#a4c5f2"
          transparent
          opacity={0.05}
          depthWrite={false}
        />
        <Edges color="#c6d9f5" />
      </mesh>
      {meshes.map(([geometry, material]) => (
        <mesh key={geometry.uuid} geometry={geometry} material={material} />
      ))}
      <mesh
        position={[...SCOUT_DECAL.position]}
        rotation={[-Math.PI / 2, 0, Math.PI]}
        material={decal}
      >
        <planeGeometry args={[...SCOUT_DECAL.size]} />
      </mesh>
    </group>
  );
}
