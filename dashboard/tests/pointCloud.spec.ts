import { expect, test } from "@playwright/test";
import {
  linearColor,
  parsePointChunk,
  PointCloudStore,
} from "../src/pointCloud";
import { liveEndpoint } from "../src/usePointCloud";

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
