export type SteeringDirection = "up" | "down" | "left" | "right";
export interface SteeringVelocity {
  v_mps: number;
  yaw_rate_rps: number;
}
const wrap = (angle: number) => Math.atan2(Math.sin(angle), Math.cos(angle));

/** viewYaw points toward the viewer along the floor (screen down). */
export function headingForDirection(
  direction: SteeringDirection,
  viewYaw: number,
) {
  const offset = {
    down: 0,
    up: Math.PI,
    right: Math.PI / 2,
    left: -Math.PI / 2,
  }[direction];
  return wrap(viewYaw + offset);
}

export function steeringVelocity(
  yaw: number,
  targetYaw: number,
): SteeringVelocity {
  if (!Number.isFinite(yaw) || !Number.isFinite(targetYaw))
    return { v_mps: 0, yaw_rate_rps: 0 };
  const error = wrap(targetYaw - yaw);
  return {
    v_mps: Math.abs(error) < 0.12 ? 0.15 : 0,
    yaw_rate_rps: Math.max(-0.5, Math.min(0.5, error * 2)),
  };
}

/** Pose feedback controls each pulse; no command queue builds up behind slow I/O. */
export class DirectionalSteering {
  private timer: ReturnType<typeof setInterval> | null = null;
  private direction: SteeringDirection | null = null;
  private abort: AbortController | null = null;
  private busy = false;
  private epoch = 0;
  constructor(
    private send: (
      body: SteeringVelocity,
      signal?: AbortSignal,
    ) => Promise<unknown>,
    private read: () => { yaw: number; viewYaw: number; ready: boolean },
    private error: (error: unknown) => void,
  ) {}
  start(direction: SteeringDirection) {
    this.direction = direction;
    if (this.timer) return;
    const epoch = ++this.epoch;
    const pulse = async () => {
      const current = this.read();
      if (!current.ready) {
        this.stop();
        return;
      }
      if (this.busy || !this.direction || epoch !== this.epoch) return;
      this.busy = true;
      const abort = new AbortController();
      this.abort = abort;
      const timeout = setTimeout(() => abort.abort(), 800);
      try {
        await this.send(
          steeringVelocity(
            current.yaw,
            headingForDirection(this.direction, current.viewYaw),
          ),
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
    this.epoch++;
    this.abort?.abort();
    this.abort = null;
    if (this.direction) {
      this.direction = null;
      void this.send({ v_mps: 0, yaw_rate_rps: 0 }).catch(() => {});
    }
  }
}
