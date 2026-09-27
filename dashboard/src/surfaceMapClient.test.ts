import { afterEach, expect, it, vi } from "vitest";
import { SurfaceMapClient } from "./surfaceMapClient";
class WorkerStub extends EventTarget {
  static latest: WorkerStub;
  sent: { id: number; retainedSurfaceIds?: string[] }[] = [];
  terminated = false;
  constructor() {
    super();
    WorkerStub.latest = this;
  }
  postMessage(data: { id: number }) {
    this.sent.push(data);
  }
  terminate() {
    this.terminated = true;
  }
  reply(id: number) {
    this.dispatchEvent(
      new MessageEvent("message", { data: { id, patch: null, cellM: 0.02 } }),
    );
  }
}
afterEach(() => vi.unstubAllGlobals());
const patch = {
  id: "test",
  positions: new Float32Array(),
  indices: new Uint32Array(),
};
it("drains aborted worker operations before accepting another patch", async () => {
  vi.stubGlobal("Worker", WorkerStub);
  const client = new SurfaceMapClient(),
    worker = WorkerStub.latest,
    abort = new AbortController();
  const first = client.add(patch, abort.signal);
  abort.abort();
  await expect(first).rejects.toThrow("cancelled");
  await expect(client.add(patch, new AbortController().signal)).rejects.toThrow(
    "busy",
  );
  expect(worker.sent).toHaveLength(1);
  worker.reply(999);
  expect(client.busy).toBe(true);
  worker.reply(1);
  expect(client.busy).toBe(false);
  const next = client.add(patch, new AbortController().signal);
  worker.reply(2);
  await expect(next).resolves.toMatchObject({ cellM: 0.02 });
  client.dispose();
});
it("source disposal rejects outstanding work and terminates its worker", async () => {
  vi.stubGlobal("Worker", WorkerStub);
  const client = new SurfaceMapClient(),
    worker = WorkerStub.latest;
  const request = client.add(patch, new AbortController().signal);
  client.dispose();
  await expect(request).rejects.toThrow("cancelled");
  expect(worker.terminated).toBe(true);
  expect(client.busy).toBe(false);
});
it("drains an aborted mesh delta before a later incremental append", async () => {
  vi.stubGlobal("Worker", WorkerStub);
  const client = new SurfaceMapClient(),
    worker = WorkerStub.latest;
  const abort = new AbortController();
  const first = client.capture(
    new ArrayBuffer(1),
    "room",
    Date.now() + 10000,
    abort.signal,
    () => {},
  );
  abort.abort();
  await expect(first).rejects.toThrow("cancelled");
  const delta = (revision: number, start: number) => ({
    revision,
    reset: !start,
    vertexStart: start,
    indexStart: start,
    vertexCount: start + 3,
    indexCount: start + 3,
    positions: new Float32Array([start, 0, 0, start + 1, 0, 0, start, 1, 0]),
    colors: new Float32Array(9).fill(0.5),
    indices: new Uint32Array([start, start + 1, start + 2]),
  });
  worker.dispatchEvent(
    new MessageEvent("message", {
      data: { id: 1, delta: delta(1, 0), cellM: 0 },
    }),
  );
  const second = client.capture(
    new ArrayBuffer(1),
    "room",
    Date.now() + 10000,
    new AbortController().signal,
    () => {},
  );
  worker.dispatchEvent(
    new MessageEvent("message", {
      data: { id: 2, delta: delta(2, 3), cellM: 0 },
    }),
  );
  const result = await second;
  expect([...result.patch!.indices]).toEqual([0, 1, 2, 3, 4, 5]);
  expect([...result.patch!.positions]).toEqual([
    0, 0, 0, 1, 0, 0, 0, 1, 0, 3, 0, 0, 4, 0, 0, 3, 1, 0,
  ]);
  client.dispose();
});
it("replays texture retirement drained after caller cancellation on the next successful reply", async () => {
  vi.stubGlobal("Worker", WorkerStub);
  const client = new SurfaceMapClient(),
    worker = WorkerStub.latest,
    abort = new AbortController();
  const first = client.capture(
    new ArrayBuffer(1),
    "room",
    Date.now() + 10000,
    abort.signal,
    () => ["old-view"],
  );
  worker.dispatchEvent(
    new MessageEvent("message", {
      data: {
        id: 1,
        preview: { surface: { ...patch, id: "old-view" }, points: {} },
      },
    }),
  );
  abort.abort();
  await expect(first).rejects.toThrow("cancelled");
  worker.dispatchEvent(
    new MessageEvent("message", {
      data: {
        id: 1,
        retiredSurfaces: [{ id: "old-view", indices: new Uint32Array() }],
        cellM: 0,
      },
    }),
  );
  const next = client.capture(
    new ArrayBuffer(1),
    "room",
    Date.now() + 10000,
    new AbortController().signal,
    () => {},
  );
  worker.reply(2);
  expect((await next).retiredSurfaces).toEqual([
    { id: "old-view", indices: new Uint32Array() },
  ]);
  client.dispose();
});

it("acknowledges actual retained texture IDs across rejected empty previews", async () => {
  vi.stubGlobal("Worker", WorkerStub);
  const client = new SurfaceMapClient(),
    worker = WorkerStub.latest;
  for (let id = 1; id <= 27; id++) {
    const pending = client.capture(
      new ArrayBuffer(1),
      "room",
      Date.now() + 10000,
      new AbortController().signal,
      () => ["still-visible"],
    );
    expect(worker.sent.at(-1)?.retainedSurfaceIds).toEqual(
      id === 1 ? [] : ["still-visible"],
    );
    worker.dispatchEvent(
      new MessageEvent("message", {
        data: {
          id,
          preview: {
            surface: {
              ...patch,
              id: id === 1 ? "still-visible" : `rejected-${id}`,
            },
            points: {},
          },
        },
      }),
    );
    worker.dispatchEvent(
      new MessageEvent("message", {
        data: {
          id,
          cellM: 0,
          retiredSurfaces:
            id === 27
              ? [{ id: "still-visible", indices: new Uint32Array() }]
              : [],
        },
      }),
    );
    const result = await pending;
    if (id === 27)
      expect(result.retiredSurfaces).toEqual([
        { id: "still-visible", indices: new Uint32Array() },
      ]);
  }
  client.dispose();
});

it.each([false, true])(
  "preserves point cleanup after caller abort; disposed=%s",
  async (disposed) => {
    vi.stubGlobal("Worker", WorkerStub);
    const { PointCloudStore } = await import("./pointCloud");
    const cloud = new PointCloudStore(16);
    const sample = {
      sessionId: "room",
      mapEpoch: 1,
      frameId: 1,
      capturedAt: 1,
      positions: new Float32Array([0, 0, -1]),
      colors: new Float32Array([1, 0, 0]),
    };
    cloud.ingestCaptured(sample);
    const view = (capturedAt: number) => ({
      sessionId: "room",
      mapEpoch: 1,
      capturedAt,
      width: 16,
      height: 16,
      depth: new Float32Array(256).fill(3),
      confidence: new Uint8Array(256).fill(2),
      projection: {
        transform: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        intrinsics: [16, 0, 0, 0, 16, 0, 8, 8, 1],
        imageWidth: 16,
        imageHeight: 16,
      },
    });
    const client = new SurfaceMapClient(),
      worker = WorkerStub.latest,
      abort = new AbortController();
    let release!: () => void;
    const acknowledgement = new Promise<void>((resolve) => (release = resolve));
    const callback = vi.fn(async (points) => {
      cloud.ingestCaptured(points);
      await acknowledgement;
    });
    const pending = client.capture(
      new ArrayBuffer(1),
      "room",
      Date.now() + 10000,
      abort.signal,
      () => [],
      -1,
      -Infinity,
      callback,
    );
    abort.abort();
    await expect(pending).rejects.toThrow("cancelled");
    if (disposed) client.dispose();
    worker.dispatchEvent(
      new MessageEvent("message", {
        data: {
          id: 1,
          cellM: 0,
          points: {
            ...sample,
            frameId: 3,
            capturedAt: 3,
            positions: new Float32Array([0, 0, -3]),
            retirement: [view(2), view(3)],
          },
        },
      }),
    );
    await Promise.resolve();
    expect([...cloud.positions.subarray(0, cloud.count * 3)]).toEqual([
      0,
      0,
      disposed ? -1 : -3,
    ]);
    expect(callback).toHaveBeenCalledTimes(disposed ? 0 : 1);
    expect(client.busy).toBe(!disposed);
    release();
    await vi.waitFor(() => expect(client.busy).toBe(false));
    client.dispose();
  },
);
