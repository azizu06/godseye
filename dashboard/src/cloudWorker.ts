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
  private announcement: unknown;
  private resume = false;
  private restore = false;

  constructor(
    private cloud: PointCloudStore,
    private onResult: (response: CloudResponse) => void,
    onError: () => void,
  ) {
    this.worker.onerror = onError;
    this.worker.onmessage = ({ data }: MessageEvent<CloudResponse>) => {
      if (data.generation !== this.generation || data.id !== this.active)
        return;
      this.active = 0;
      if (data.update) this.cloud.applyUpdate(data.update);
      this.onResult(data);
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
  reset() {
    this.resume = this.restore = false;
    this.generation++;
    this.pending = this.announcement = undefined;
    this.pendingCapture = undefined;
    this.cloud.clear();
    this.send("clear");
  }
  announce(value: unknown) {
    this.announcement = value;
    this.pending = undefined;
    this.pendingCapture = undefined;
    this.flush();
  }
  ingest(value: unknown) {
    this.pending = value;
    this.flush();
  }
  ingestCaptured(value: CapturedPoints) {
    this.pendingCapture = value;
    this.flush();
  }
  private flush() {
    if (this.active) return;
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
  private send(kind: CloudRequest["kind"], value?: unknown) {
    this.active = ++this.sequence;
    const message: CloudRequest = {
      generation: this.generation,
      id: this.active,
      kind,
      value,
    };
    this.worker.postMessage(
      message,
      kind === "capture"
        ? [
            (value as CapturedPoints).positions.buffer,
            (value as CapturedPoints).colors.buffer,
            ...((value as CapturedPoints).covered
              ? [(value as CapturedPoints).covered!.buffer]
              : []),
          ]
        : value instanceof ArrayBuffer
          ? [value]
          : [],
    );
  }
  dispose() {
    this.worker.terminate();
  }
}
