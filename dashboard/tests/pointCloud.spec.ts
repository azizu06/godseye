import { expect, test } from "@playwright/test";
import {
  MAX_POINTS,
  linearColor,
  parsePointChunk,
  PointCloudStore,
  type CapturedPoints,
} from "../src/pointCloud";
import { liveEndpoint } from "../src/usePointCloud";
import { binaryPoints } from "./support/binaryPoints";

const chunk = (id = 1, positions = [1, 2, -3], colors = [1, 0.5, 0]) => ({
  version: 1,
  type: "points",
  session_id: "phone-a",
  map_epoch: 1,
  chunk_id: id,
  t_capture: id,
  positions,
  colors,
});

test("covered points leave the GPU draw list while original measurements and incremental updates survive", () => {
  const worker = new PointCloudStore(64),
    renderer = new PointCloudStore(64);
  const positions = Float32Array.from({ length: 64 * 3 }, (_, i) =>
    i % 3 === 0 ? Math.floor(i / 3) * 0.02 : 0,
  );
  const colors = new Float32Array(positions.length).fill(0.5);
  for (let frameId = 1; frameId <= 30; frameId++) {
    const covered = Uint8Array.from({ length: 64 }, (_, i) =>
      Number((i + frameId) % 7 !== 0),
    );
    worker.ingestCaptured({
      sessionId: "draw",
      mapEpoch: 1,
      frameId,
      capturedAt: frameId,
      positions,
      colors,
      covered,
    });
    renderer.applyUpdate(worker.takeUpdate());
    const expected = [...covered.keys()].filter((i) => covered[i] === 0);
    expect(
      [...renderer.visibleIndices.subarray(0, renderer.visibleCount)].sort(
        (a, b) => a - b,
      ),
    ).toEqual(expected);
    expect(renderer.count).toBe(64);
    expect(renderer.positions).toEqual(positions);
  }
  worker.clear();
  renderer.applyUpdate(worker.takeUpdate());
  expect(renderer.visibleCount).toBe(0);
  worker.ingest(chunk(1));
  renderer.applyUpdate(worker.takeUpdate());
  expect([
    ...renderer.visibleIndices.subarray(0, renderer.visibleCount),
  ]).toEqual([0]);
});

test("late coverage does not hide newer foreground points and recycled slots regain visibility", () => {
  const cloud = new PointCloudStore(2);
  cloud.ingest(chunk(10, [0.008, 0, 0]));
  cloud.ingestCaptured({
    sessionId: "phone-a",
    mapEpoch: 1,
    frameId: 9,
    capturedAt: 9,
    positions: new Float32Array([0.001, 0, 0, 1, 0, 0]),
    colors: new Float32Array(6),
    covered: new Uint8Array([1, 1]),
  });
  expect(cloud.visibleCount).toBe(1);
  expect(cloud.visibleIndices[0]).toBe(0);
  cloud.ingest(chunk(11, [2, 0, 0]));
  cloud.ingest(chunk(12, [3, 0, 0]));
  expect(cloud.count).toBe(2);
  expect(cloud.visibleCount).toBe(2);
  expect(new Set(cloud.visibleIndices.subarray(0, 2)).size).toBe(2);
});

test("native captures fill missing cells without rewinding newer wire measurements or double-converting color", () => {
  const cloud = new PointCloudStore(8);
  cloud.ingest(chunk(10, [0.003, 0, 0], [1, 0, 0]));
  const capture: CapturedPoints = {
    sessionId: "phone-a",
    mapEpoch: 1,
    frameId: 1,
    capturedAt: 9,
    positions: new Float32Array([0.004, 0, 0, 1.003, 0, 0]),
    colors: new Float32Array([0, 0, 1, 0.214041, 0, 0]),
  };
  expect(cloud.ingestCaptured(capture)).toBe("accepted");
  expect(cloud.count).toBe(2);
  expect(cloud.positions[0]).toBeCloseTo(0.003);
  expect(cloud.colors[0]).toBe(1);
  expect(cloud.positions[3]).toBeCloseTo(1.003);
  expect(cloud.colors[3]).toBeCloseTo(0.214041);
  expect(cloud.ingestCaptured(capture)).toBe("ignored");
  expect(cloud.ingestCaptured({ ...capture, frameId: 2, capturedAt: 10 })).toBe(
    "accepted",
  );
  expect(cloud.positions[0]).toBeCloseTo(0.004);
  expect(cloud.colors[2]).toBe(1);
  const positions = cloud.positions.slice();
  expect(
    cloud.ingestCaptured({
      ...capture,
      sessionId: "bad",
      positions: new Float32Array([NaN, 0, 0]),
    }),
  ).toBe("invalid");
  expect(cloud.positions).toEqual(positions);
  cloud.announce({
    version: 1,
    type: "objects",
    session_id: "phone-a",
    map_epoch: 2,
  });
  expect(cloud.ingestCaptured(capture)).toBe("ignored");
  expect(cloud.ingestCaptured({ ...capture, mapEpoch: 2 })).toBe("accepted");
  expect(cloud.positions[0]).toBeCloseTo(0.004);
});

test("world coordinates are preserved and JPEG colors are converted to linear RGB", () => {
  const cloud = new PointCloudStore(4);
  expect(cloud.ingest(chunk())).toBe("accepted");
  expect([...cloud.positions.slice(0, 3)]).toEqual([1, 2, -3]);
  expect(cloud.colors[0]).toBe(1);
  expect(cloud.colors[1]).toBeCloseTo(0.214041, 5);
  expect(cloud.colors[2]).toBe(0);
  expect(linearColor(0)).toBe(0);
  expect(linearColor(1)).toBe(1);
});

test("reobserving a voxel replaces its measurement without moving to a voxel center", () => {
  const cloud = new PointCloudStore(4);
  cloud.ingest(chunk(1, [0.003, 0.004, 0.005]));
  cloud.ingest(chunk(2, [0.008, 0.009, 0.007], [0, 0, 1]));
  expect(cloud.count).toBe(1);
  expect(cloud.positions[0]).toBeCloseTo(0.008);
  expect([...cloud.colors.slice(0, 3)]).toEqual([0, 0, 1]);
});

test("point memory is bounded and framing uses only retained geometry", () => {
  const cloud = new PointCloudStore(2);
  cloud.ingest(
    chunk(1, [10, 0, 0, 20, 0, 0, 30, 0, 0], [1, 0, 0, 0, 1, 0, 0, 0, 1]),
  );
  expect(cloud.count).toBe(2);
  expect(cloud.evicted).toBe(1);
  expect(cloud.positions.length).toBe(6);
  expect(cloud.bounds()).toEqual({ center: [25, 0, 0], radius: 5 });
  cloud.clear();
  expect(cloud.count).toBe(0);
  expect(cloud.bounds()).toBeNull();
});

test("invalid packets cannot clear or partially overwrite the displayed map", () => {
  const cloud = new PointCloudStore(4);
  cloud.ingest(chunk());
  for (const changes of [
    { positions: [1, 2] },
    { colors: [2, 0, 0] },
    { colors: [0, 1] },
    { positions: [Infinity, 0, 0] },
    { positions: [1e20, 0, 0] },
    { positions: Array(7503).fill(1), colors: Array(7503).fill(1) },
    { map_epoch: 0 },
    { t_capture: NaN },
    { version: 2 },
    { chunk_id: -1 },
  ]) {
    expect(
      cloud.ingest({ ...chunk(2), session_id: "invalid-new-map", ...changes }),
    ).toBe("invalid");
    expect(cloud.count).toBe(1);
    expect([...cloud.positions.slice(0, 3)]).toEqual([1, 2, -3]);
  }
});

test("duplicate and delayed points are ignored; new epochs clear the map", () => {
  const cloud = new PointCloudStore(4);
  cloud.ingest(chunk(5));
  expect(cloud.ingest(chunk(5))).toBe("ignored");
  expect(cloud.ingest(chunk(4))).toBe("ignored");
  expect(cloud.ingest({ ...chunk(1, [9, 0, 0]), map_epoch: 2 })).toBe(
    "accepted",
  );
  expect(cloud.count).toBe(1);
  expect(cloud.positions[0]).toBe(9);
  expect(cloud.ingest(chunk(99))).toBe("ignored");
  expect(cloud.positions[0]).toBe(9);
});

test("map announcements clear old geometry before new points arrive", () => {
  const cloud = new PointCloudStore(4);
  cloud.ingest(chunk());
  expect(
    cloud.announce({
      version: 1,
      type: "objects",
      session_id: "phone-b",
      map_epoch: 1,
    }),
  ).toBe(true);
  expect(cloud.count).toBe(0);
  expect(cloud.ingest(chunk(2))).toBe("ignored");
  expect(cloud.ingest({ ...chunk(), session_id: "phone-b" })).toBe("accepted");
});

test("phone reconnect may restart chunk IDs while capture time keeps increasing", () => {
  const cloud = new PointCloudStore(4);
  cloud.ingest(chunk(50));
  expect(cloud.ingest({ ...chunk(1), t_capture: 51 })).toBe("accepted");
  cloud.clear();
  expect(cloud.ingest(chunk(1))).toBe("accepted");
});

test("legacy v1 point chunks remain connection-scoped and cannot mix into identified maps", () => {
  const cloud = new PointCloudStore(4);
  const legacy = {
    version: 1,
    type: "points",
    chunk_id: 1,
    positions: [0, 0, 0],
    colors: [1, 0, 0],
  };
  expect(parsePointChunk(legacy)).not.toBeNull();
  expect(cloud.ingest(legacy)).toBe("accepted");
  expect(cloud.ingest(legacy)).toBe("ignored");
  cloud.ingest(chunk());
  expect(cloud.ingest({ ...legacy, chunk_id: 2 })).toBe("ignored");
  expect(cloud.count).toBe(1);
});

test("feed URLs follow the viewing laptop and allow an explicit source", () => {
  expect(liveEndpoint("http://10.0.0.8:5173/")).toBe("ws://10.0.0.8:8765/live");
  expect(liveEndpoint("https://example.test/")).toBe(
    "wss://example.test:8765/live",
  );
  expect(liveEndpoint("http://localhost:5173/?live=off")).toBeNull();
  expect(
    liveEndpoint(
      "http://localhost:5173/?live=ws%3A%2F%2Fexample.test%3A9999%2Flive",
    ),
  ).toBe("ws://example.test:9999/live");
  expect(() =>
    liveEndpoint("http://localhost:5173/?live=https://example.test/live"),
  ).toThrow();
});

test("a configured backend is shared across browsers and explicit URLs still win", () => {
  const configured = "ws://10.0.0.44:8765/live";
  for (const page of ["http://localhost:5173/", "http://10.0.0.68:5173/"])
    expect(liveEndpoint(page, configured)).toBe(configured);
  expect(
    liveEndpoint("http://localhost:5173/?live=off", configured),
  ).toBeNull();
  expect(
    liveEndpoint(
      "http://localhost:5173/?live=ws%3A%2F%2Fother.test%3A8765%2Flive",
      configured,
    ),
  ).toBe("ws://other.test:8765/live");
  expect(liveEndpoint("http://localhost:5173/", "   ")).toBe(
    "ws://localhost:8765/live",
  );
  expect(() =>
    liveEndpoint("http://localhost:5173/", "https://bad.test"),
  ).toThrow();
});

test("relative feed URLs use the dashboard server for the shared relay", () => {
  expect(liveEndpoint("http://10.0.0.68:5173/", "/live")).toBe(
    "ws://10.0.0.68:5173/live",
  );
  expect(liveEndpoint("https://scan.test/", "/live")).toBe(
    "wss://scan.test/live",
  );
  expect(liveEndpoint("http://localhost:5173/?live=/live")).toBe(
    "ws://localhost:5173/live",
  );
  expect(() =>
    liveEndpoint("http://localhost:5173/", "//other.test/live"),
  ).toThrow();
});

test("point buffer retains two million distinct samples and uploads appended ranges only", () => {
  const cloud = new PointCloudStore();
  expect(MAX_POINTS).toBe(2_000_000);
  const colors = Array(7500).fill(0.5);
  for (let offset = 0; offset < MAX_POINTS; offset += 2500) {
    const positions = [];
    for (let i = offset; i < offset + 2500; i++)
      positions.push((i % 2000) * 0.02, Math.floor(i / 2000) * 0.02, 0);
    cloud.ingest({
      version: 1,
      type: "points",
      session_id: "phone-a",
      map_epoch: 1,
      chunk_id: offset + 1,
      positions,
      colors,
    });
    expect(cloud.takeUpdateRanges()).toEqual([
      { start: offset * 3, count: 7500 },
    ]);
  }
  expect(cloud.count).toBe(2_000_000);
  expect(cloud.evicted).toBe(0);
  cloud.ingest(chunk(MAX_POINTS + 1, [90, 90, 90]));
  expect(cloud.count).toBe(2_000_000);
  expect(cloud.evicted).toBe(1);
  cloud.takeUpdateRanges();
  const scattered = [];
  for (let i = 1; i <= 2500; i++) {
    const j = i * 797;
    scattered.push((j % 2000) * 0.02, Math.floor(j / 2000) * 0.02, 0);
  }
  cloud.ingest(chunk(MAX_POINTS + 2, scattered, Array(7500).fill(0.9)));
  // Scattered observations used to force all 48 MB back onto the GPU.
  expect(
    cloud.takeUpdateRanges().reduce((sum, r) => sum + r.count * 8, 0),
  ).toBe(60_000);
});

test("dense binary packets keep measured coordinates, color precision, and reject malformed data atomically", () => {
  const cloud = new PointCloudStore(4);
  expect(cloud.ingest(binaryPoints([1.123456, 2, -3], [17, 128, 255]))).toBe(
    "accepted",
  );
  expect(cloud.positions[0]).toBeCloseTo(1.123456, 6);
  expect(cloud.colors[0]).toBeCloseTo(linearColor(17 / 255), 6);
  expect(cloud.colors[1]).toBeCloseTo(linearColor(128 / 255), 6);
  expect(cloud.colors[2]).toBe(1);
  for (const packet of [
    binaryPoints([1, 2, 3], [1, 2, 3], { count: 20_001 }),
    binaryPoints([1, 2, 3], [1, 2, 3], { colors: "unknown" }),
    binaryPoints([NaN, 2, 3], [1, 2, 3]),
    binaryPoints([1, 2, 3], [1, 2, 3], { map_epoch: 0 }),
    binaryPoints([1, 2, 3], [1, 2, 3], { t_capture: -1 }),
    binaryPoints([1, 2, 3], [1, 2, 3]).slice(0, -1),
    new ArrayBuffer(8),
  ]) {
    expect(cloud.ingest(packet)).toBe("invalid");
    expect(cloud.count).toBe(1);
    expect(cloud.positions[0]).toBeCloseTo(1.123456, 6);
  }
});

test("worker patches match the cache, preserve map resets, and skip unchanged GPU data", () => {
  const worker = new PointCloudStore(4),
    renderer = new PointCloudStore(4);
  worker.ingest(
    binaryPoints([0.001, 1, -2, 2000, 1, -2], [128, 64, 0, 0, 128, 64]),
  );
  renderer.applyUpdate(worker.takeUpdate());
  expect(renderer.count).toBe(2);
  expect(renderer.positions).toEqual(worker.positions);
  expect(renderer.colors).toEqual(worker.colors);
  renderer.takeUpdateRanges();
  worker.ingest(
    binaryPoints([0.001, 1, -2, 2000, 1, -2], [128, 64, 0, 0, 128, 64], {
      t_capture: 2,
      chunk_id: 2,
    }),
  );
  expect(worker.takeUpdate().positions.length).toBe(0);
  worker.announce({
    version: 1,
    type: "objects",
    session_id: "next-map",
    map_epoch: 1,
  });
  renderer.applyUpdate(worker.takeUpdate());
  expect(renderer.count).toBe(0);
  expect(renderer.bounds()).toBeNull();
  expect(
    worker.ingest(binaryPoints([0, 1, 2], [255, 0, 0], { t_capture: 3 })),
  ).toBe("ignored");
});
