import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { capture } from "./captureFixture";
import { closeWorkspace, workspaceAction } from "./helpers";

function packet(frame: number) {
  const person = frame === 2;
  // Keep a distinct recent textured view: a camera translation prevents the
  // ordinary same-view replacement from masking missing depth retirement.
  const body = capture(1, 2, frame, person ? 0.3 : 0);
  if (person) {
    const length = body.readUInt32LE(0);
    const header = JSON.parse(body.toString("utf8", 4, 4 + length));
    const offset =
      4 +
      length +
      header.sections.find((s: { name: string }) => s.name === "raw_depth")
        .offset;
    for (let y = 4; y < 11; y++)
      for (let x = 6; x < 14; x++)
        body.writeFloatLE(1, offset + (y * 20 + x) * 4);
    // An isolated native measurement cannot form a triangle. It makes the
    // default visible point layer independently responsible for one ghost.
    body.writeFloatLE(1, offset + (7 * 20 + 2) * 4);
  }
  return body;
}

async function drawnGeometry(page: Page) {
  return page.evaluate(async () => {
    const moduleUrl = "/node_modules/.vite/deps/@react-three_fiber.js";
    const { _roots } = await import(moduleUrl);
    const root = _roots.get(document.querySelector("canvas"));
    if (!root)
      return {
        foregroundFaces: 0,
        foregroundPoints: 0,
        wallFaces: 0,
        recentForegroundFaces: 0,
      };
    const { scene } = root.store.getState();
    let foregroundFaces = 0,
      foregroundPoints = 0,
      wallFaces = 0,
      recentForegroundFaces = 0;
    scene.traverse((object: any) => {
      if (!object.visible || !object.geometry) return;
      const points = object.name === "live-point-cloud";
      const surface =
        object.isMesh && object.parent?.name === "observed-color-surfaces";
      if (!points && !surface) return;
      const geometry = object.geometry,
        p = geometry.attributes.position,
        index = geometry.index;
      const count = Math.min(geometry.drawRange.count, index?.count ?? p.count);
      const start = geometry.drawRange.start;
      // Inspect the exact referenced draw range, not unused buffer capacity,
      // exported counters or app status. This catches a stale rendered patch.
      for (let i = start; i < start + count; i += points ? 1 : 3) {
        const ids = points
          ? [index ? index.getX(i) : i]
          : [0, 1, 2].map((j) => (index ? index.getX(i + j) : i + j));
        const depths = ids.map((id) => p.getZ(id));
        if (depths.every((z) => z > -1.3 && z < -0.7)) {
          if (points) foregroundPoints++;
          else {
            foregroundFaces++;
            if (object.material.map) recentForegroundFaces++;
          }
        }
        if (surface && depths.every((z) => Math.abs(z + 2) < 0.001))
          wallFaces++;
      }
    });
    return {
      foregroundFaces,
      foregroundPoints,
      wallFaces,
      recentForegroundFaces,
    };
  });
}

test("two newer background captures remove colored moving silhouettes from actual mesh, texture and point draws", async ({
  page,
}) => {
  test.setTimeout(90000);
  let frame = 1,
    committedFrame = 0;
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  // No real backend, phone or rover route can be reached by this test.
  await page.routeWebSocket(/ws:\/\/(localhost|127\.0\.0\.1):8765\/.*/, (ws) =>
    ws.close(),
  );
  await page.route(/http:\/\/(localhost|127\.0\.0\.1):8765\/.*/, (route) =>
    route.fulfill({ status: 404 }),
  );
  await page.routeWebSocket("ws://localhost:9878/live", (ws) => {
    const publish = () => {
      ws.send(
        JSON.stringify({
          version: 1,
          type: "objects",
          session_id: "surface-room",
          map_epoch: 1,
          objects: [],
        }),
      );
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone: "ok",
          car: "down",
          detector: "down",
          pose_age_ms: 0,
          mode: "manual",
          armed: false,
          stop_reason: null,
          session_id: "surface-room",
          map_epoch: 1,
        }),
      );
    };
    publish();
    const timer = setInterval(publish, 100);
    ws.onClose(() => clearInterval(timer));
  });
  await page.route("http://localhost:9878/**", (route) => {
    if (new URL(route.request().url()).pathname === "/capture/surface.bin") {
      const tag = `"dynamic-${frame}"`;
      if (route.request().headers()["if-none-match"] === tag) {
        committedFrame = frame;
        return route.fulfill({ status: 304, headers: { ETag: tag } });
      }
      return route.fulfill({
        body: packet(frame),
        contentType: "application/octet-stream",
        headers: {
          ETag: tag,
          "X-Capture-Age-Ms": "10",
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Expose-Headers": "ETag, X-Capture-Age-Ms",
        },
      });
    }
    return route.fulfill({
      json: {
        version: 1,
        session_id: "surface-room",
        map_epoch: 1,
        objects: [],
        events: [],
        baselines: [],
      },
    });
  });
  await page.goto(
    "/?live=ws%3A%2F%2Flocalhost%3A9878%2Flive&api=http%3A%2F%2Flocalhost%3A9878",
  );
  await page.getByRole("button", { name: "3D", exact: true }).click();
  await expect
    .poll(async () => (await drawnGeometry(page)).wallFaces)
    .toBeGreaterThan(0);
  await expect.poll(() => committedFrame).toBe(1);
  frame = 2;
  await expect
    .poll(async () => (await drawnGeometry(page)).foregroundFaces)
    .toBeGreaterThan(0);
  await expect
    .poll(async () => (await drawnGeometry(page)).recentForegroundFaces)
    .toBeGreaterThan(0);
  await expect
    .poll(async () => (await drawnGeometry(page)).foregroundPoints)
    .toBeGreaterThan(0);
  await page.screenshot({ path: "/tmp/godseye-dynamic-scan-before.png" });
  await expect.poll(() => committedFrame).toBe(2);
  frame = 3;
  // Wait for the final worker reply/ETag commit, not merely the HTTP request.
  await expect.poll(() => committedFrame).toBe(3);
  expect((await drawnGeometry(page)).foregroundFaces).toBeGreaterThan(0);
  frame = 4;
  await expect
    .poll(
      async () => {
        const drawn = await drawnGeometry(page);
        return drawn.foregroundFaces + drawn.foregroundPoints;
      },
      { timeout: 30000 },
    )
    .toBe(0);
  expect((await drawnGeometry(page)).wallFaces).toBeGreaterThan(0);
  await page.screenshot({ path: "/tmp/godseye-dynamic-scan-after.png" });
  const download = page.waitForEvent("download");
  await workspaceAction(page, "Export snapshot");
  const json = JSON.parse(
    await readFile((await (await download).path())!, "utf8"),
  );
  const map = json.colored_reconstruction;
  expect(map.indices.length).toBeGreaterThan(0);
  for (const index of map.indices)
    expect(map.positions[index * 3 + 2]).toBeCloseTo(-2, 3);
  await closeWorkspace(page);
  expect(errors).toEqual([]);
});
