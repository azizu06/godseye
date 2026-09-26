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
