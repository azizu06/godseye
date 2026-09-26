import { expect, test } from "@playwright/test";
import { SurfaceFeed } from "../src/surfaceFeed";

const signal = () => new AbortController().signal;
const binary = (age = "10", tag = '"frame-1"') =>
  new Response(new Uint8Array([1, 2, 3]), {
    headers: { "X-Capture-Age-Ms": age, ETag: tag },
  });
const status = (rich = false) => ({
  tracking: "normal",
  tracking_lost_capture: 3,
  health: { phone: "ok" },
  frame: { capture_id: "v1", age_ms: 20, metadata: { t_capture: 4 } },
  rich: {
    packets: rich
      ? {
          frame: {
            token: "v2",
            age_ms: 20,
            t_capture: 4,
            sections: ["rgb", "raw_depth", "raw_confidence"].map((name) => ({
              name,
            })),
          },
        }
      : {},
  },
});

test("compact preview uses one request and skips unchanged bodies", async () => {
  const calls: { url: string; headers: Headers }[] = [];
  const feed = new SurfaceFeed("http://preview.test", async (input, init) => {
    calls.push({ url: String(input), headers: new Headers(init?.headers) });
    return calls.length === 1 ? binary() : new Response(null, { status: 304 });
  });
  const first = await feed.read(signal());
  expect(new Uint8Array(first!.buffer)).toEqual(new Uint8Array([1, 2, 3]));
  expect(first!.expiresAt).toBeGreaterThan(performance.now());
  expect(await feed.read(signal())).toBeNull();
  expect(calls.map((c) => c.url)).toEqual(
    Array(2).fill("http://preview.test/capture/surface.bin"),
  );
  expect(calls[1].headers.get("If-None-Match")).toBe('"frame-1"');
});

for (const rich of [false, true]) {
  test(`older servers fall back once and select calibrated ${rich ? "v2" : "v1"} packets`, async () => {
    const calls: string[] = [];
    const feed = new SurfaceFeed("http://preview.test", async (input) => {
      const path = new URL(String(input)).pathname;
      calls.push(path);
      if (path === "/capture/surface.bin")
        return new Response(null, { status: 404 });
      if (path === "/capture/status") return Response.json(status(rich));
      return binary();
    });
    expect((await feed.read(signal()))!.trackingLostCapture).toBe(3);
    expect(await feed.read(signal())).toBeNull();
    expect(calls).toEqual([
      "/capture/surface.bin",
      "/capture/status",
      rich ? "/capture/rich/frame.bin" : "/capture/frame.bin",
      "/capture/status",
    ]);
  });
}

test("empty, stale, and invalid-age previews cannot enter the map", async () => {
  for (const response of [
    new Response(null, { status: 204 }),
    binary("1001"),
    binary("NaN"),
    binary("-1"),
  ]) {
    const feed = new SurfaceFeed("http://preview.test", async () => response);
    expect(await feed.read(signal())).toBeNull();
  }
});

test("failed and oversized responses do not trigger full-archive downloads", async () => {
  for (const response of [
    new Response(null, { status: 503 }),
    new Response(new Uint8Array([1]), {
      headers: {
        "Content-Length": String(33 * 1024 * 1024),
        "X-Capture-Age-Ms": "0",
      },
    }),
  ]) {
    let calls = 0;
    const feed = new SurfaceFeed("http://preview.test", async () => {
      calls++;
      return response;
    });
    await expect(feed.read(signal())).rejects.toThrow("Capture unavailable");
    expect(calls).toBe(1);
  }
});

test("aborted responses are discarded before decode", async () => {
  const controller = new AbortController();
  const feed = new SurfaceFeed("http://preview.test", async () => {
    controller.abort();
    return binary();
  });
  await expect(feed.read(controller.signal)).rejects.toThrow();
});
