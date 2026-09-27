export interface ManualCapabilities {
  forward: [number, number] | null;
  reverse: [number, number] | null;
  yaw: [number, number] | null;
  arcs: boolean;
}
export const defaultManualCapabilities: ManualCapabilities = {
  forward: [0, 0.15],
  reverse: [0, 0.15],
  yaw: [0, 0.5],
  arcs: true,
};
export function joystickVector(
  x: number,
  y: number,
  capabilities: ManualCapabilities = defaultManualCapabilities,
) {
  if (!Number.isFinite(x) || !Number.isFinite(y))
    return { x: 0, y: 0, v: 0, w: 0 };
  const radius = Math.hypot(x, y);
  if (radius <= 0.12) return { x: 0, y: 0, v: 0, w: 0 };
  const magnitude = (Math.min(1, radius) - 0.12) / 0.88;
  x = (x / radius) * magnitude;
  y = (y / radius) * magnitude;
  if (!capabilities.yaw) x = 0;
  if ((y < 0 && !capabilities.forward) || (y > 0 && !capabilities.reverse))
    y = 0;
  if (!capabilities.arcs) {
    if (Math.abs(x) > Math.abs(y)) y = 0;
    else x = 0;
  }
  const scale = (value: number, range: [number, number] | null) =>
    !value || !range
      ? 0
      : Math.sign(value) * (range[0] + Math.abs(value) * (range[1] - range[0]));
  return {
    x,
    y,
    v: scale(-y, y < 0 ? capabilities.forward : capabilities.reverse),
    w: scale(-x, capabilities.yaw),
  };
}

export function validManualCapabilities(
  value: unknown,
): value is ManualCapabilities | null {
  if (value === null) return true;
  if (!value || typeof value !== "object") return false;
  const caps = value as ManualCapabilities;
  const range = (r: unknown, max: number) =>
    r === null ||
    (Array.isArray(r) &&
      r.length === 2 &&
      r.every((v) => typeof v === "number" && Number.isFinite(v)) &&
      r[0] >= 0 &&
      r[1] >= r[0] &&
      r[1] <= max);
  return (
    typeof caps.arcs === "boolean" &&
    range(caps.forward, 0.2) &&
    range(caps.reverse, 0.2) &&
    range(caps.yaw, 0.5)
  );
}
