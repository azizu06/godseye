import { simulator, panel, workspaceAction, closeWorkspace } from "./helpers";
import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { capture } from "./captureFixture";

type ExportedMap = { positions: number[]; colors: number[]; indices: number[] };
function verifyCompressedCapture(map: ExportedMap) {
  // The unchanged 20×15 depth fixture has 300 vertices and 532 triangles.
  expect(map.positions.length / 3).toBeLessThan(300);
  expect(map.indices.length / 3).toBeLessThan(532);
  expect(map.indices.length).toBeGreaterThan(0);
  expect(map.colors.length).toBe(map.positions.length);
  const xs = map.positions.filter((_, i) => i % 3 === 0);
  const ys = map.positions.filter((_, i) => i % 3 === 1);
  expect(Math.min(...xs)).toBeCloseTo(-1.9, 2);
  expect(Math.max(...xs)).toBeCloseTo(1.9, 2);
  expect(Math.min(...ys)).toBeCloseTo(-0.4, 2);
  expect(Math.max(...ys)).toBeCloseTo(2.4, 2);
  for (let i = 2; i < map.positions.length; i += 3)
    expect(map.positions[i]).toBeCloseTo(-2, 3);
  let area = 0;
  const triangles: { ids: number[]; xy: number[][]; twiceArea: number }[] = [];
  for (let i = 0; i < map.indices.length; i += 3) {
    const ids = map.indices.slice(i, i + 3);
    const xy = ids.map((id) => map.positions.slice(id * 3, id * 3 + 2));
    const [a, b, c] = xy;
    const twiceArea =
      (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    // The observed wall faces the camera (+Z); remeshing must preserve winding.
    expect(twiceArea).toBeGreaterThan(0);
    area += twiceArea / 2;
    triangles.push({ ids, xy, twiceArea });
  }
  expect(area).toBeCloseTo(10.64, 2); // 3.8 m × 2.8 m observed footprint.
  const sample = (x: number, y: number) => {
    for (const {
      ids,
      xy: [a, b, c],
      twiceArea,
    } of triangles) {
      const weights = [
        ((b[0] - x) * (c[1] - y) - (b[1] - y) * (c[0] - x)) / twiceArea,
        ((c[0] - x) * (a[1] - y) - (c[1] - y) * (a[0] - x)) / twiceArea,
        ((a[0] - x) * (b[1] - y) - (a[1] - y) * (b[0] - x)) / twiceArea,
      ];
      if (weights.every((weight) => weight >= -1e-6))
        return [0, 1, 2].map((channel) =>
          weights.reduce(
            (sum, weight, i) => sum + weight * map.colors[ids[i] * 3 + channel],
            0,
          ),
        );
    }
    return null;
  };
  // Sample all four measured JPEG quadrants, not just a retained corner color.
  // RGB literals are independently read from the unchanged fixture's image.
  for (const [x, y, rgb] of [
    [-1, 1.75, [190, 90, 56]],
    [1, 1.75, [53, 107, 154]],
    [-1, 0.25, [224, 193, 100]],
    [1, 0.25, [100, 161, 120]],
  ] as [number, number, number[]][]) {
    const color = sample(x, y);
    expect(color, `observed color at (${x}, ${y})`).not.toBeNull();
    rgb.forEach((byte, channel) => {
      const encoded = byte / 255;
      const linear =
        encoded <= 0.04045
          ? encoded / 12.92
          : ((encoded + 0.055) / 1.055) ** 2.4;
      expect(Math.abs(color![channel] - linear)).toBeLessThan(0.06);
    });
  }
  // Interior footprint coverage survives around the quadrant boundaries too.
  for (let y = -0.3; y < 2.4; y += 0.2)
    for (let x = -1.8; x < 1.9; x += 0.2) expect(sample(x, y)).not.toBeNull();
}
test("default solid surfaces discover progressively and point cloud remains optional", async ({
  page,
}) => {
  await simulator(page);
  await panel(page, "Scene settings");
  await expect(page.getByTestId("surface-status")).toContainText(
    /color triangles/,
  );
  await expect(page.getByTestId("surface-status")).toContainText(
    "Coarse preview",
  );
  await page.getByRole("button", { name: "Scene layers", exact: true }).click();
  await expect(
    page.getByLabel("Color surfaces", { exact: true }),
  ).toBeChecked();
  await expect(
    page.getByLabel("Point cloud", { exact: true }),
  ).not.toBeChecked();
  await page.getByLabel("Point cloud", { exact: true }).check();
  await page.getByLabel("Color surfaces", { exact: true }).uncheck();
  await expect(page.getByTestId("surface-status")).toHaveCount(0);
  await page.getByLabel("Color surfaces", { exact: true }).check();
  await page.getByLabel("Point cloud", { exact: true }).uncheck();
  await page.getByRole("button", { name: "Scene layers", exact: true }).click();
  await page.waitForTimeout(5000);
  await page.screenshot({ path: "/tmp/godseye-color-surfaces.png" });
});
for (const version of [1, 2])
  test(`existing v${version} binary capture renders without drive permission and rejects late frames after a map reset`, async ({
    page,
  }) => {
    let epoch = 1;
    let release = () => {};
    let pending = false;
    const gate = new Promise<void>((r) => (release = r));
    await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
      const send = () => {
        ws.send(
          JSON.stringify({
            version: 1,
            type: "health",
            phone: "ok",
            car: "down",
            detector: "ok",
            pose_age_ms: 0,
            mode: "manual",
            armed: false,
            stop_reason: null,
            session_id: "surface-room",
            map_epoch: epoch,
          }),
        );
        ws.send(
          JSON.stringify({
            version: 1,
            type: "pose",
            position: [0, 1, 0],
            yaw_rad: Math.PI,
            tracking: "normal",
            session_id: "surface-room",
            map_epoch: epoch,
          }),
        );
      };
      send();
      const id = setInterval(send, 100);
      ws.onClose(() => clearInterval(id));
    });
    await page.route("http://localhost:9876/**", async (route) => {
      if (route.request().url().endsWith("/capture/status"))
        return route.fulfill({
          json: {
            tracking: "normal",
            rich:
              version === 2
                ? {
                    packets: {
                      frame: {
                        token: `rich-${epoch}`,
                        age_ms: 10,
                        sections: ["rgb", "raw_depth", "raw_confidence"].map(
                          (name) => ({ name }),
                        ),
                      },
                    },
                  }
                : undefined,
            frame: { capture_id: epoch === 1 ? "one" : "two", age_ms: 10 },
          },
        });
      if (
        route
          .request()
          .url()
          .endsWith(
            version === 2 ? "/capture/rich/frame.bin" : "/capture/frame.bin",
          )
      ) {
        if (epoch === 2) {
          pending = true;
          await gate;
        }
        return route.fulfill({
          body: capture(1, version),
          contentType: "application/octet-stream",
        });
      }
      return route.fulfill({ json: { version: 1, ok: true } });
    });
    await simulator(page);
    await workspaceAction(page, "Connection settings");
    await page.getByRole("button", { name: /External feed Connect/ }).click();
    await page.getByLabel("Enable REST commands").check();
    await page
      .getByLabel("Telemetry WebSocket")
      .fill("ws://localhost:9876/live");
    await page.getByLabel("Backend API base").fill("http://localhost:9876");
    await page.getByLabel("Enable REST commands").uncheck();
    await expect(page.getByLabel("Backend API base")).toBeVisible();
    await page
      .getByRole("button", { name: "Connect source", exact: true })
      .click();
    await closeWorkspace(page);
    await panel(page, "Scene settings");
    await expect(page.getByTestId("surface-status")).toContainText(
      "color triangles",
    );
    await expect(page.getByTestId("surface-status")).toContainText(
      "Coarse preview",
    );
    await expect(page.locator(".scene-stat > span")).toHaveText("MAP VERTICES");
    const download = page.waitForEvent("download");
    await workspaceAction(page, "Export snapshot");
    const snapshot = JSON.parse(
      await readFile((await (await download).path())!, "utf8"),
    );
    await panel(page, "Scene settings");
    expect(snapshot.colored_reconstruction).not.toBeNull();
    verifyCompressedCapture(snapshot.colored_reconstruction);
    await expect(page.locator(".scene-stat > strong")).toHaveText(
      (snapshot.colored_reconstruction.positions.length / 3).toLocaleString(),
    );
    await page.waitForTimeout(800);
    await page.screenshot({
      path: `/tmp/godseye-planar-capture-v${version}.png`,
    });
    epoch = 2;
    await expect.poll(() => pending).toBe(true);
    await expect(page.getByTestId("surface-status")).not.toContainText(
      "color triangles",
    );
    await expect(page.locator(".scene-stat > span")).toHaveText(
      "POINTS RECEIVED",
    );
    release();
    await page.waitForTimeout(500);
    await expect(page.getByTestId("surface-status")).not.toContainText(
      "color triangles",
    );
  });
