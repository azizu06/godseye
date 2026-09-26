import {
  type ChangeEvent,
  type Health,
  type Message,
  type Mode,
  type Vec3,
  type WorldObject,
} from "./protocol";

export const ROOM = { width: 8, depth: 6 };
export const FURNITURE: { position: Vec3; size: Vec3; color: string }[] = [
  { position: [-2.7, 0.37, -1.9], size: [2.1, 0.74, 0.9], color: "#547c87" },
  { position: [-2.7, 0.75, -2.26], size: [2.1, 0.7, 0.16], color: "#547c87" },
  { position: [1.45, 0.68, -1.9], size: [2.15, 0.08, 1.05], color: "#a68a67" },
  { position: [1.45, 0.31, -1.9], size: [0.08, 0.62, 0.08], color: "#547c87" },
  { position: [-2.7, 0.38, 0.3], size: [1.4, 0.08, 0.9], color: "#677f88" },
  { position: [3.5, 0.8, 1.9], size: [0.5, 1.6, 1.5], color: "#557682" },
];
export function makeRoomPoints() {
  const positions: number[] = [],
    colors: number[] = [];
  let seed = 42;
  const random = () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  const add = (x: number, y: number, z: number, tone: number) => {
    positions.push(
      x + (random() - 0.5) * 0.025,
      y + (random() - 0.5) * 0.018,
      z + (random() - 0.5) * 0.025,
    );
    const bright = 0.65 + random() * 0.35;
    colors.push(
      (0.22 + tone * 0.24) * bright,
      (0.5 + tone * 0.17) * bright,
      (0.54 + tone * 0.13) * bright,
    );
  };
  for (let x = -4; x <= 4; x += 0.075)
    for (let z = -3; z <= 3; z += 0.075) if (random() > 0.13) add(x, 0, z, 0.2);
  for (let y = 0.05; y < 2.3; y += 0.07) {
    for (let x = -4; x <= 4; x += 0.07) {
      if (!(x > -0.6 && x < 0.6 && y < 1.8)) add(x, y, -3, 0.5);
    }
    for (let z = -3; z <= 3; z += 0.07) add(-4, y, z, 0.1);
  }
  for (const f of FURNITURE)
    for (let x = -f.size[0] / 2; x <= f.size[0] / 2; x += 0.055)
      for (let z = -f.size[2] / 2; z <= f.size[2] / 2; z += 0.055)
        add(
          f.position[0] + x,
          f.position[1] + f.size[1] / 2,
          f.position[2] + z,
          0.8,
        );
  return { positions, colors };
}
export class Simulator {
  elapsed = 0;
  position: Vec3 = [0.1, 0.16, 1.7];
  yaw = Math.PI;
  health: Health = {
    phone: "ok",
    car: "ok",
    detector: "ok",
    pose_age_ms: 12,
    mode: "manual",
    armed: false,
    stop_reason: "SIMULATION · awaiting operator",
  };
  objects: WorldObject[] = [];
  events: ChangeEvent[] = [];
  path: [number, number][] = [];
  tracking = true;
  private velocity = 0;
  private turn = 0;
  private lastManual = -Infinity;
  private rescanAt: number | null = null;
  private moved = false;
  private origin = Date.now() / 1000;
  constructor() {
    this.reset();
  }
  private reset() {
    this.elapsed = 0;
    this.position = [0.1, 0.16, 1.7];
    this.yaw = Math.PI;
    this.velocity = 0;
    this.turn = 0;
    this.lastManual = -Infinity;
    this.path = [];
    this.rescanAt = null;
    this.moved = false;
    this.tracking = true;
    this.origin = Date.now() / 1000;
    this.health = {
      phone: "ok",
      car: "ok",
      detector: "ok",
      pose_age_ms: 12,
      mode: "manual",
      armed: false,
      stop_reason: "SIMULATION · awaiting operator",
    };
    const initial: [string, Vec3, number][] = [
      ["backpack", [1.4, 0.45, 0.5], 0.97],
      ["chair", [1.4, 0.44, -0.85], 0.94],
      ["potted plant", [-3.2, 0.65, 2.1], 0.96],
      ["bottle", [-2.6, 0.6, 0.3], 0.91],
      ["laptop", [1.25, 0.8, -1.9], 0.98],
    ];
    this.objects = initial.map(([cls, position, confidence], i) => ({
      id: `sim-${cls.replace("potted ", "")}`,
      class: cls,
      position,
      confidence,
      first_seen: this.origin - 45 + i * 5,
      last_seen: this.origin,
      observations: 18 + i * 7,
      state: "present",
    }));
    this.events = this.objects.map((o, i) => ({
      kind: "new",
      object_id: o.id,
      new_position: o.position,
      t: this.origin - 45 + i * 5,
    }));
  }
  setTracking(ok: boolean) {
    this.tracking = ok;
    this.health.phone = ok ? "ok" : "stale";
    this.health.pose_age_ms = ok ? 12 : 1800;
    if (!ok) this.stop("Tracking lost");
    else
      this.health.stop_reason =
        "SIMULATION · tracking restored; rearm required";
  }
  private stop(reason: string) {
    this.health.armed = false;
    this.health.stop_reason = reason;
    this.velocity = 0;
    this.turn = 0;
    this.path = [];
    this.rescanAt = null;
  }
  command(path: string, body: Record<string, unknown> = {}) {
    switch (path) {
      case "/session":
        this.reset();
        break;
      case "/stop":
        this.stop("Operator stop");
        this.rescanAt = null;
        break;
      case "/arm":
        if (this.rescanAt !== null)
          throw Error("Wait for the rescan to complete before arming.");
        if (!this.tracking)
          throw Error("Tracking must be normal before arming.");
        this.health.armed = true;
        this.health.stop_reason = null;
        break;
      case "/mode":
        if (!["manual", "navigate", "explore"].includes(String(body.mode)))
          throw Error("Unknown mode");
        this.stop("Mode changed; rearm required");
        this.health.mode = body.mode as Mode;
        break;
      case "/manual": {
        if (!this.health.armed || this.health.mode !== "manual")
          throw Error("Arm in Manual mode before driving.");
        const v = Number(body.v_mps),
          w = Number(body.yaw_rate_rps);
        if (
          !Number.isFinite(v) ||
          !Number.isFinite(w) ||
          Math.abs(v) > 0.2 ||
          Math.abs(w) > 0.5
        )
          throw Error("Command exceeds speed limits.");
        this.velocity = v;
        this.turn = w;
        this.lastManual = this.elapsed;
        break;
      }
      case "/goal": {
        if (!this.health.armed || this.health.mode !== "navigate")
          throw Error("Arm in Navigate mode before choosing a goal.");
        const x = Number(body.x),
          z = Number(body.z);
        if (
          !Number.isFinite(x) ||
          !Number.isFinite(z) ||
          Math.abs(x) > 3.5 ||
          Math.abs(z) > 2.5
        )
          throw Error("Choose a goal inside the simulated room.");
        this.path = [
          [this.position[0], this.position[2]],
          [x, z],
        ];
        break;
      }
      case "/rescan":
        if (!this.tracking) throw Error("Restore tracking before rescanning.");
        if (this.health.armed)
          throw Error("Stop the rover before starting the relocation demo.");
        this.rescanAt = this.elapsed + 2.5;
        this.health.stop_reason = "SIMULATION · revisiting baseline";
        break;
      case "/ask":
        return {
          answer:
            "The local simulator supports spatial inspection. Natural-language queries need backend integration.",
        };
      default:
        throw Error("This operation is not available.");
    }
    return { version: 1, ok: true };
  }
  tick(dt: number) {
    this.elapsed += dt;
    if (this.health.armed && this.tracking) {
      if (
        this.health.mode === "manual" &&
        this.elapsed - this.lastManual <= 0.3
      ) {
        this.yaw += this.turn * dt;
        this.position = [
          Math.max(
            -3.7,
            Math.min(
              3.7,
              this.position[0] + Math.sin(this.yaw) * this.velocity * dt,
            ),
          ),
          0.16,
          Math.max(
            -2.7,
            Math.min(
              2.7,
              this.position[2] + Math.cos(this.yaw) * this.velocity * dt,
            ),
          ),
        ];
      }
      if (this.health.mode === "navigate" && this.path.length) {
        const goal = this.path.at(-1)!,
          dx = goal[0] - this.position[0],
          dz = goal[1] - this.position[2],
          distance = Math.hypot(dx, dz);
        if (distance < 0.03) this.stop("Simulated goal reached");
        else {
          const step = Math.min(distance, 0.2 * dt);
          this.yaw = Math.atan2(dx, dz);
          this.position = [
            this.position[0] + (dx / distance) * step,
            0.16,
            this.position[2] + (dz / distance) * step,
          ];
        }
      }
      if (this.health.mode === "explore") {
        this.yaw += 0.15 * dt;
        this.position = [
          Math.max(
            -3.6,
            Math.min(3.6, this.position[0] + Math.sin(this.yaw) * 0.15 * dt),
          ),
          0.16,
          Math.max(
            -2.6,
            Math.min(2.6, this.position[2] + Math.cos(this.yaw) * 0.15 * dt),
          ),
        ];
      }
    }
    if (this.rescanAt !== null && this.elapsed >= this.rescanAt) {
      const bag = this.objects[0],
        old = [...bag.position] as Vec3;
      bag.position = this.moved ? [1.4, 0.45, 0.5] : [-0.2, 0.45, 0.5];
      bag.state = "moved";
      bag.observations += 3;
      bag.last_seen = this.origin + this.elapsed;
      this.events.push({
        kind: "moved",
        object_id: bag.id,
        old_position: old,
        new_position: [...bag.position],
        displacement_m: 1.6,
        t: this.origin + this.elapsed,
      });
      this.moved = !this.moved;
      this.rescanAt = null;
      this.health.stop_reason = "SIMULATION · rescan complete";
    }
  }
  snapshot(includeRoom = false): Message[] {
    const messages: Message[] = [
      { version: 1, type: "health", ...this.health },
      {
        version: 1,
        type: "pose",
        position: [...this.position],
        yaw_rad: this.yaw,
        tracking: this.tracking ? "normal" : "limited",
      },
      {
        version: 1,
        type: "objects",
        objects: this.objects.map((o) => ({ ...o, position: [...o.position] })),
      },
      { version: 1, type: "path", points: this.path.map((p) => [...p]) },
      ...this.events.map((e) => ({
        version: 1 as const,
        type: "event" as const,
        ...e,
      })),
    ];
    if (includeRoom) {
      messages.push({
        version: 1,
        type: "points",
        chunk_id: 1,
        ...makeRoomPoints(),
      });
      const cells = Array.from({ length: 80 * 60 }, (_, i) => {
        const x = i % 80,
          z = Math.floor(i / 80);
        return x === 0 || x === 79 || z === 0 || z === 59
          ? 2
          : x > 65 && z < 12
            ? 0
            : 1;
      });
      messages.push({
        version: 1,
        type: "occupancy",
        origin: [-4, -3],
        cell_m: 0.1,
        width: 80,
        height: 60,
        cells: btoa(String.fromCharCode(...cells)),
      });
    }
    return messages;
  }
}
