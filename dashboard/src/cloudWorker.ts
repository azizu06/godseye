import {
  PointCloudStore,
  type CloudUpdate,
  type CapturedPoints,
} from "./pointCloud";

export type CloudRequest = {
  generation: number;
  id: number;
  kind: "clear" | "announce" | "ingest" | "capture" | "reconnect" | "restore";
  value?: unknown;
};
export type CloudResponse = {
  generation: number;
  id: number;
  kind: CloudRequest["kind"];
  result: "accepted" | "ignored" | "invalid";
  update?: CloudUpdate;
};

/** One in-flight job, newest wire chunk and newest RGB-D frame; bounded latency. */
export class CloudWorker {
  private worker = new Worker(
    new URL("./pointCloud.worker.ts", import.meta.url),
    { type: "module" },
  );
  private generation = 0;
  private sequence = 0;
  private active = 0;
  private pending: unknown;
  private pendingCapture: CapturedPoints | undefined;
  private retirementQueue: {
    value: CapturedPoints;
    resolve: (accepted: boolean) => void;
  }[] = [];
  private activeRetirement?: (accepted: boolean) => void;
  private failed = false;
  private disposed = false;
  private announcement: unknown;
  private resume = false;
  private restore = false;

  constructor(
    private cloud: PointCloudStore,
    private onResult: (response: CloudResponse) => void,
    private onError: () => void,
  ) {
    this.worker.onerror = () => this.fail();
    this.worker.onmessage = ({ data }: MessageEvent<CloudResponse>) => {
      if (data.generation !== this.generation || data.id !== this.active)
        return;
      this.active = 0;
      const acknowledge = this.activeRetirement;
      this.activeRetirement = undefined;
      if (data.update) this.cloud.applyUpdate(data.update);
      this.onResult(data);
      acknowledge?.(data.result !== "invalid");
      this.flush();
    };
  }
  reconnect() {
    this.resume = true;
    this.flush();
  }
  restoreCoverage() {
    this.restore = true;
    this.flush();
  }
  private fail() {
    this.failed = true;
    this.active = 0;
    this.settleRetirements();
    this.pending = this.pendingCapture = undefined;
    this.onError();
  }
  private settleRetirements() {
    this.activeRetirement?.(false);
    this.activeRetirement = undefined;
    for (const job of this.retirementQueue) job.resolve(false);
    this.retirementQueue = [];
  }
  reset() {
    this.settleRetirements();
    this.resume = this.restore = false;
    this.generation++;
    this.pending = this.announcement = undefined;
    this.pendingCapture = undefined;
    this.cloud.clear();
    this.send("clear");
  }
  announce(value: unknown) {
    this.settleRetirements();
    this.announcement = value;
    this.pending = undefined;
    this.pendingCapture = undefined;
    this.flush();
  }
  ingest(value: unknown) {
    this.pending = value;
    this.flush();
  }
  ingestCaptured(value: CapturedPoints): Promise<boolean> {
    if (this.failed || this.disposed) return Promise.resolve(false);
    if (!value.retirement) {
      this.pendingCapture = value;
      this.flush();
      return Promise.resolve(true);
    }
    // Final producers await acknowledgement before requesting another frame.
    // Keep this guard explicit in case a caller violates that bounded flow.
    if (this.retirementQueue.length >= 8) {
      this.onError();
      return Promise.resolve(false);
    }
    // Other consumers still own these rasters. Only private copies transfer.
    const owned = {
      ...value,
      retirement: value.retirement.map((o) => ({
        ...o,
        depth: o.depth.slice(),
        confidence: o.confidence.slice(),
      })),
    };
    return new Promise((resolve) => {
      this.retirementQueue.push({ value: owned, resolve });
      this.flush();
    });
  }
  private flush() {
    if (this.active || this.failed || this.disposed) return;
    if (this.resume) {
      this.resume = false;
      this.send("reconnect");
    } else if (this.restore) {
      this.restore = false;
      this.send("restore");
    } else if (this.announcement !== undefined) {
      const value = this.announcement;
      this.announcement = undefined;
      this.send("announce", value);
    } else if (this.retirementQueue.length) {
      const job = this.retirementQueue.shift()!;
      this.send("capture", job.value, job.resolve);
    } else if (this.pendingCapture !== undefined) {
      const value = this.pendingCapture;
      this.pendingCapture = undefined;
      this.send("capture", value);
    } else if (this.pending !== undefined) {
      const value = this.pending;
      this.pending = undefined;
      this.send("ingest", value);
    }
  }
  private send(
    kind: CloudRequest["kind"],
    value?: unknown,
    acknowledge?: (accepted: boolean) => void,
  ) {
    this.activeRetirement = acknowledge;
    this.active = ++this.sequence;
    const message: CloudRequest = {
      generation: this.generation,
      id: this.active,
      kind,
      value,
    };
    try {
      this.worker.postMessage(
        message,
        kind === "capture"
          ? [
              (value as CapturedPoints).positions.buffer,
              (value as CapturedPoints).colors.buffer,
              ...((value as CapturedPoints).retirement ?? []).flatMap((o) => [
                o.depth.buffer,
                o.confidence.buffer,
              ]),
              ...((value as CapturedPoints).covered
                ? [(value as CapturedPoints).covered!.buffer]
                : []),
            ]
          : value instanceof ArrayBuffer
            ? [value]
            : [],
      );
    } catch {
      this.fail();
    }
  }
  dispose() {
    this.disposed = true;
    this.settleRetirements();
    this.pending = this.pendingCapture = undefined;
    this.worker.terminate();
  }
}
