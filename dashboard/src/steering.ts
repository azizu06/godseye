export type SteeringDirection = "up" | "down" | "left" | "right";
export interface SteeringVelocity {
  v_mps: number;
  yaw_rate_rps: number;
}
const wrap = (angle: number) => Math.atan2(Math.sin(angle), Math.cos(angle));

/** A new press chooses a heading relative to the rover, independent of the camera. */
export function headingForDirection(
  direction: SteeringDirection,
  roverYaw: number,
) {
  const offset = {
    down: Math.PI,
    up: 0,
    right: -Math.PI / 2,
    left: Math.PI / 2,
  }[direction];
  // Preserve forward exactly: wrapping an already valid heading introduces tiny
  // numerical yaw corrections before the first forward pulse.
  return direction === "up" ? roverYaw : wrap(roverYaw + offset);
}

export function steeringVelocity(
  yaw: number,
  targetYaw: number,
): SteeringVelocity {
  if (!Number.isFinite(yaw) || !Number.isFinite(targetYaw))
    return { v_mps: 0, yaw_rate_rps: 0 };
  const error = wrap(targetYaw - yaw);
  if (Math.abs(error) < 0.02) return { v_mps: 0.15, yaw_rate_rps: 0 };
  return {
    v_mps: 0,
    yaw_rate_rps: Math.max(-0.5, Math.min(0.5, error * 2)),
  };
}

/** Pose feedback controls each pulse; no command queue builds up behind slow I/O. */
export class DirectionalSteering {
  private timer: ReturnType<typeof setInterval> | null = null;
  private direction: SteeringDirection | null = null;
  private targetYaw: number | null = null;
  private abort: AbortController | null = null;
  private busy = false;
  private epoch = 0;
  constructor(
    private send: (
      body: SteeringVelocity,
      signal?: AbortSignal,
    ) => Promise<unknown>,
    private read: () => { yaw: number; ready: boolean },
    private error: (error: unknown) => void,
  ) {}
  start(direction: SteeringDirection) {
    const current = this.read();
    if (!current.ready || !Number.isFinite(current.yaw)) {
      this.stop();
      return;
    }
    // A held key must not move the target as the rover turns. Repeated starts
    // preserve it; changing direction or releasing and pressing chooses anew.
    if (direction !== this.direction || this.targetYaw === null)
      this.targetYaw = headingForDirection(direction, current.yaw);
    this.direction = direction;
    if (this.timer) return;
    const epoch = ++this.epoch;
    const pulse = async () => {
      const current = this.read();
      if (!current.ready) {
        this.stop();
        return;
      }
      if (this.busy || this.targetYaw === null || epoch !== this.epoch) return;
      this.busy = true;
      const abort = new AbortController();
      this.abort = abort;
      const timeout = setTimeout(() => abort.abort(), 800);
      try {
        await this.send(
          steeringVelocity(current.yaw, this.targetYaw),
          abort.signal,
        );
      } catch (error) {
        if (epoch === this.epoch) {
          this.stop();
          this.error(error);
        }
      } finally {
        clearTimeout(timeout);
        this.busy = false;
      }
    };
    this.timer = setInterval(() => void pulse(), 100);
    void pulse();
  }
  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.targetYaw = null;
    this.epoch++;
    this.abort?.abort();
    this.abort = null;
    if (this.direction) {
      this.direction = null;
      void this.send({ v_mps: 0, yaw_rate_rps: 0 }).catch(() => {});
    }
  }
}
