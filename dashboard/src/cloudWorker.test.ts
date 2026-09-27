import { afterEach, expect, test, vi } from "vitest";
import {
  CloudWorker,
  type CloudRequest,
  type CloudResponse,
} from "./cloudWorker";
import { PointCloudStore, type CapturedPoints } from "./pointCloud";

class TestWorker {
  static latest: TestWorker;
  messages: CloudRequest[] = [];
  onmessage?: (message: { data: CloudResponse }) => void;
  onerror?: () => void;
  constructor() {
    TestWorker.latest = this;
  }
  postMessage(message: CloudRequest, transfer: Transferable[]) {
    this.messages.push(structuredClone(message, { transfer }));
  }
  terminate() {}
  reply(
    request: CloudRequest,
    result: CloudResponse["result"] = "accepted",
    update?: CloudResponse["update"],
  ) {
    this.onmessage?.({ data: { ...request, result, update } });
  }
}
afterEach(() => vi.unstubAllGlobals());
const capture = (): CapturedPoints => ({
  sessionId: "s",
  mapEpoch: 1,
  frameId: 3,
  capturedAt: 3,
  positions: new Float32Array([0, 0, -3]),
  colors: new Float32Array(3),
  retirement: [2, 3].map((capturedAt) => ({
    sessionId: "s",
    mapEpoch: 1,
    capturedAt,
    width: 2,
    height: 2,
    depth: new Float32Array(4).fill(3),
    confidence: new Uint8Array(4).fill(2),
    projection: {
      transform: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
      intrinsics: [2, 0, 0, 0, 2, 0, 1, 1, 1],
      imageWidth: 2,
      imageHeight: 2,
    },
  })),
});

test("point worker transfers private raster copies while recent surface retirement keeps caller evidence", () => {
  vi.stubGlobal("Worker", TestWorker);
  const client = new CloudWorker(
    new PointCloudStore(4),
    () => {},
    () => {},
  );
  const value = capture();
  client.ingestCaptured(value);
  expect(value.retirement![0].depth.byteLength).toBe(16);
  expect(value.retirement![1].confidence.byteLength).toBe(4);
  const received = TestWorker.latest.messages[0].value as CapturedPoints;
  expect([...received.retirement![0].depth]).toEqual([3, 3, 3, 3]);
  client.dispose();
});

test("reset invalidates an in-flight retirement and discards pending old-scope capture", () => {
  vi.stubGlobal("Worker", TestWorker);
  const cloud = new PointCloudStore(4),
    onResult = vi.fn();
  const client = new CloudWorker(cloud, onResult, () => {}),
    worker = TestWorker.latest;
  client.ingestCaptured(capture());
  const old = worker.messages[0];
  client.ingestCaptured(capture());
  client.reset();
  const clear = worker.messages[1];
  const staleCloud = new PointCloudStore(4);
  staleCloud.ingestCaptured({ ...capture(), retirement: undefined });
  worker.reply(old, "accepted", staleCloud.takeUpdate());
  expect(cloud.count).toBe(0);
  expect(onResult).not.toHaveBeenCalled();
  worker.reply(clear);
  expect(worker.messages).toHaveLength(2);
  expect(onResult).toHaveBeenCalledOnce();
  client.dispose();
});

test("retirement jobs survive later preview coalescing and acknowledge only after worker completion", async () => {
  vi.stubGlobal("Worker", TestWorker);
  const client = new CloudWorker(
      new PointCloudStore(8),
      () => {},
      () => {},
    ),
    worker = TestWorker.latest;
  client.ingestCaptured({ ...capture(), retirement: undefined });
  const first = worker.messages[0];
  let resolved = false;
  const result = client.ingestCaptured(capture()).then((value) => {
    resolved = true;
    return value;
  });
  client.ingestCaptured({
    ...capture(),
    frameId: 4,
    capturedAt: 4,
    retirement: undefined,
  });
  await Promise.resolve();
  expect(resolved).toBe(false);
  worker.reply(first);
  expect((worker.messages[1].value as CapturedPoints).retirement).toHaveLength(
    2,
  );
  expect(resolved).toBe(false);
  worker.reply(worker.messages[1]);
  expect(await result).toBe(true);
  expect((worker.messages[2].value as CapturedPoints).capturedAt).toBe(4);
  client.dispose();
});

test("reset and worker errors settle both active and queued cleanup acknowledgements", async () => {
  vi.stubGlobal("Worker", TestWorker);
  const client = new CloudWorker(
      new PointCloudStore(8),
      () => {},
      () => {},
    ),
    worker = TestWorker.latest;
  const active = client.ingestCaptured(capture()),
    queued = client.ingestCaptured(capture());
  client.reset();
  expect(await active).toBe(false);
  expect(await queued).toBe(false);
  worker.reply(worker.messages.at(-1)!);
  const failed = client.ingestCaptured(capture());
  worker.onerror?.();
  expect(await failed).toBe(false);
  client.dispose();
});

test("cleanup queue overflow is explicit and leaves every accepted job intact", async () => {
  vi.stubGlobal("Worker", TestWorker);
  const onError = vi.fn();
  const client = new CloudWorker(new PointCloudStore(8), () => {}, onError),
    worker = TestWorker.latest;
  const accepted = Array.from({ length: 9 }, () =>
    client.ingestCaptured(capture()),
  );
  expect(await client.ingestCaptured(capture())).toBe(false);
  expect(onError).toHaveBeenCalledOnce();
  for (let i = 0; i < 9; i++) worker.reply(worker.messages[i]);
  expect(await Promise.all(accepted)).toEqual(new Array(9).fill(true));
  expect(worker.messages).toHaveLength(9);
  client.dispose();
});

test("dispose and map announcement settle queued cleanup instead of hanging a producer", async () => {
  vi.stubGlobal("Worker", TestWorker);
  const client = new CloudWorker(
    new PointCloudStore(8),
    () => {},
    () => {},
  );
  const active = client.ingestCaptured(capture()),
    queued = client.ingestCaptured(capture());
  client.announce({
    version: 1,
    type: "objects",
    session_id: "other",
    map_epoch: 1,
    objects: [],
  });
  expect(await active).toBe(false);
  expect(await queued).toBe(false);
  TestWorker.latest.reply(TestWorker.latest.messages[0]);
  TestWorker.latest.reply(TestWorker.latest.messages[1]);
  const next = client.ingestCaptured(capture());
  client.dispose();
  expect(await next).toBe(false);
  expect(await client.ingestCaptured(capture())).toBe(false);
});

test("a transfer exception settles cleanup and reports worker failure", async () => {
  vi.stubGlobal("Worker", TestWorker);
  const onError = vi.fn();
  const client = new CloudWorker(new PointCloudStore(8), () => {}, onError);
  TestWorker.latest.postMessage = () => {
    throw new Error("transfer failed");
  };
  expect(await client.ingestCaptured(capture())).toBe(false);
  expect(onError).toHaveBeenCalledOnce();
  client.dispose();
});
