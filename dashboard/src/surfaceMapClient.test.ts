import { afterEach, expect, it, vi } from "vitest";
import { SurfaceMapClient } from "./surfaceMapClient";
class WorkerStub extends EventTarget {
  static latest: WorkerStub;
  sent: { id: number }[] = [];
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
