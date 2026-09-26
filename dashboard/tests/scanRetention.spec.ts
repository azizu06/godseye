import {
  observedFeed,
  panel,
  workspaceAction,
  closeWorkspace,
} from "./helpers";
import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { capture } from "./captureFixture";
test("whole colored scan outlives recent views, phone loss and same-map reconnect; exports then clears on new map", async ({
  page,
}) => {
  test.setTimeout(120000);
  let frame = 1,
    enabled = true,
    phone = "ok",
    epoch = 1,
    connections = 0;
  let close = () => {},
    confirm = () => {},
    publishScope = () => {};
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    connections++;
    const health = () =>
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone,
          car: "down",
          detector: "down",
          pose_age_ms: phone === "ok" ? 10 : null,
          mode: "manual",
          armed: false,
          stop_reason: phone === "ok" ? null : "phone_disconnected",
        }),
      );
    publishScope = () =>
      ws.send(
        JSON.stringify({
          version: 1,
          type: "objects",
          session_id: "surface-room",
          map_epoch: epoch,
          objects: [],
        }),
      );
    confirm = publishScope;
    close = () => ws.close();
    health();
    if (connections === 1) publishScope();
    else
      ws.send(
        JSON.stringify({
          version: 1,
          type: "objects",
          session_id: null,
          map_epoch: null,
          objects: [],
        }),
      );
    const id = setInterval(health, 100);
    ws.onClose(() => clearInterval(id));
  });
  await page.route("http://localhost:9876/**", async (route) => {
    if (route.request().url().endsWith("/capture/status"))
      return route.fulfill({
        json: {
          tracking: phone === "ok" ? "normal" : null,
          frame: { capture_id: `${frame}`, age_ms: enabled ? 10 : 16000 },
        },
      });
    if (route.request().url().endsWith("/capture/frame.bin")) {
      const current = frame++;
      if (frame > 32) enabled = false;
      return route.fulfill({
        body: capture(epoch, 1, current, (current - 1) * 0.4),
        contentType: "application/octet-stream",
      });
    }
    return route.fulfill({
      json: {
        version: 1,
        session_id: "surface-room",
        map_epoch: epoch,
        events: [],
      },
    });
  });
  await observedFeed(page);
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9876/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9876");
  await page.getByLabel("Enable REST commands").uncheck();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await panel(page, "Scene settings");
  await expect.poll(() => frame, { timeout: 90000 }).toBe(33);
  await expect(page.getByTestId("surface-status")).toContainText(
    "capture paused",
  );
  const count = await page
    .getByTestId("surface-status")
    .getAttribute("data-map-triangles");
  expect(Number(count)).toBeGreaterThan(0);
  await expect(
    page.getByRole("heading", { name: "Waiting for a view of the world" }),
  ).toHaveCount(0);
  await page.screenshot({ path: "/tmp/godseye-persistent-scan.png" });
  const download = page.waitForEvent("download");
  await workspaceAction(page, "Export snapshot");
  const json = JSON.parse(
    await readFile((await (await download).path())!, "utf8"),
  );
  await panel(page, "Scene settings");
  const map = json.colored_reconstruction;
  expect(map).not.toBeNull();
  expect(map.indices.length).toBeGreaterThan(0);
  // Compression retains the full sweep with fewer vertices than its raw views.
  expect(map.positions.length / 3).toBeLessThan(32 * 300);
  await expect(page.locator(".scene-stat > span")).toHaveText("MAP VERTICES");
  await expect(page.locator(".scene-stat > strong")).toHaveText(
    (map.positions.length / 3).toLocaleString(),
  );
  let min = Infinity,
    max = -Infinity,
    oldRed = false;
  for (let i = 0; i < map.positions.length; i += 3) {
    const x = map.positions[i];
    min = Math.min(min, x);
    max = Math.max(max, x);
    if (
      x < -1.8 &&
      map.positions[i + 1] > 1.5 &&
      map.colors[i] > map.colors[i + 1] * 2 &&
      map.colors[i] > map.colors[i + 2] * 2
    )
      oldRed = true;
  }
  expect(min).toBeLessThan(-1.8);
  expect(max).toBeGreaterThan(13);
  expect(oldRed).toBe(true);
  phone = "down";
  await page.waitForTimeout(300);
  await expect(page.getByTestId("surface-status")).toHaveAttribute(
    "data-map-triangles",
    count!,
  );
  close();
  await expect.poll(() => connections).toBe(2);
  await expect(page.getByTestId("surface-status")).toHaveAttribute(
    "data-map-triangles",
    count!,
  );
  confirm();
  await page.waitForTimeout(400);
  await expect(page.getByTestId("surface-status")).toHaveAttribute(
    "data-map-triangles",
    count!,
  );
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Enable REST commands").check();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await panel(page, "Scene settings");
  await expect(page.getByTestId("surface-status")).toHaveAttribute(
    "data-map-triangles",
    count!,
  );
  expect(connections).toBe(2);
  epoch = 2;
  publishScope();
  await expect(page.getByTestId("surface-status")).toHaveAttribute(
    "data-map-triangles",
    "0",
  );
  await expect(page.locator(".scene-stat > span")).toHaveText(
    "POINTS RECEIVED",
  );
});
