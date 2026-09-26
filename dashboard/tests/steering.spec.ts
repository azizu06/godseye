import {
  observedFeed,
  panel,
  workspace,
  workspaceAction,
  closeWorkspace,
} from "./helpers";
import { test, expect } from "@playwright/test";
for (const interrupt of ["release", "stop"] as const) {
  test(`Standard handoff cannot move after ${interrupt} while Arm is pending`, async ({
    page,
  }) => {
    let mode = "navigate",
      armed = false,
      arms = 0,
      stops = 0,
      release: () => void = () => {},
      started: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r)),
      armStarted = new Promise<void>((r) => (started = r));
    const motion: { v_mps: number; yaw_rate_rps: number }[] = [];
    await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
      const update = () => {
        ws.send(
          JSON.stringify({
            version: 1,
            type: "health",
            phone: "ok",
            car: "ok",
            detector: "ok",
            pose_age_ms: 1,
            mode,
            armed,
            stop_reason: null,
          }),
        );
        ws.send(
          JSON.stringify({
            version: 1,
            type: "pose",
            position: [0, 0.16, 0],
            yaw_rad: Math.PI,
            tracking: "normal",
          }),
        );
      };
      update();
      const id = setInterval(update, 100);
      ws.onClose(() => clearInterval(id));
    });
    await page.route("http://localhost:9876/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/arm") {
        arms++;
        if (arms === 2) {
          started();
          await gate;
        }
        armed = true;
      }
      if (path === "/mode") {
        mode = route.request().postDataJSON().mode;
        armed = false;
      }
      if (path === "/stop") {
        stops++;
        armed = false;
      }
      if (path === "/manual") motion.push(route.request().postDataJSON());
      await route.fulfill({ json: { version: 1, armed, mode } });
    });
    await observedFeed(page);
    await page.getByRole("button", { name: "2D", exact: true }).click();
    await workspaceAction(page, "Connection settings");
    await page.getByLabel("Enable REST commands").check();
    await page
      .getByLabel("Telemetry WebSocket")
      .fill("ws://localhost:9876/live");
    await page.getByLabel("Backend API base").fill("http://localhost:9876");
    await page
      .getByRole("button", { name: "Connect source", exact: true })
      .click();
    await closeWorkspace(page);
    await page.getByRole("button", { name: "Arm rover", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "STOP ROVER", exact: true }),
    ).toBeEnabled();
    // Stop is intentionally available before the Arm acknowledgement/health
    // arrives. Wait for actual armed telemetry before requesting the handoff.
    await expect(page.locator(".operator-panel .state-pill")).toHaveText(
      "Armed",
    );
    await page.keyboard.down("ArrowDown");
    await armStarted;
    await expect(
      page.getByRole("button", { name: "STOP ROVER", exact: true }),
    ).toBeEnabled();
    if (interrupt === "release") await page.keyboard.up("ArrowDown");
    else
      await page
        .getByRole("button", { name: "STOP ROVER", exact: true })
        .click();
    release();
    await expect
      .poll(() => stops)
      .toBeGreaterThanOrEqual(interrupt === "stop" ? 2 : 1);
    await page.keyboard.up("ArrowDown");
    await expect.poll(() => armed).toBe(false);
    expect(
      motion.filter((m) => m.v_mps !== 0 || m.yaw_rate_rps !== 0),
    ).toHaveLength(0);
  });
}
test("map fills the viewport, retained panels are optional, and search does not steer", async ({
  page,
}) => {
  await observedFeed(page);
  await expect(page.locator(".workspace-drawer")).toHaveCount(0);
  const viewport = page.viewportSize()!;
  const bounds = (await page.locator(".scene-canvas").boundingBox())!;
  expect(bounds.width).toBe(viewport.width);
  expect(bounds.height).toBe(viewport.height);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await panel(page, "Spatial memory");
  await page.getByRole("textbox", { name: "Search objects" }).focus();
  const before = await page
    .locator(".map2d > g[transform]")
    .getAttribute("transform");
  await page.keyboard.down("ArrowDown");
  await page.waitForTimeout(350);
  await page.keyboard.up("ArrowDown");
  expect(
    await page.locator(".map2d > g[transform]").getAttribute("transform"),
  ).toBe(before);
  await panel(page, "Recent activity");
  await expect(
    page.getByRole("heading", { name: "Recent activity" }),
  ).toBeVisible();
  await panel(page, "Object intelligence");
  await expect(page.locator(".inspector-empty")).toBeVisible();
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await workspace(page);
  await expect(
    page.getByRole("button", { name: "Export snapshot", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "New session", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("failed Arm response reasserts Stop after the backend applied it", async ({
  page,
}) => {
  let armed = false,
    stops = 0;
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    const update = () => {
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone: "ok",
          car: "ok",
          detector: "ok",
          pose_age_ms: 0,
          mode: "manual",
          armed,
          stop_reason: null,
        }),
      );
      ws.send(
        JSON.stringify({
          version: 1,
          type: "pose",
          position: [0, 0.16, 0],
          yaw_rad: 0,
          tracking: "normal",
        }),
      );
    };
    update();
    const timer = setInterval(update, 100);
    ws.onClose(() => clearInterval(timer));
  });
  await page.route("http://localhost:9876/**", (route) => {
    if (route.request().url().endsWith("/arm")) {
      armed = true;
      return route.fulfill({
        status: 500,
        json: { detail: "Arm response failed" },
      });
    }
    if (route.request().url().endsWith("/stop")) {
      stops++;
      armed = false;
    }
    return route.fulfill({ json: { version: 1, armed } });
  });
  await observedFeed(page);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9876/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9876");
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await expect.poll(() => stops).toBe(1);
  expect(armed).toBe(false);
  await expect(page.getByRole("status")).toContainText("Arm response failed");
});

test("compact Stop remains available while reconnect has no current health", async ({
  page,
}) => {
  let connections = 0,
    stops = 0;
  let disconnect = () => {};
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    connections++;
    disconnect = () => ws.close();
    if (connections === 1)
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone: "ok",
          car: "ok",
          detector: "ok",
          pose_age_ms: 0,
          mode: "explore",
          armed: true,
          stop_reason: null,
        }),
      );
  });
  await page.route("http://localhost:9876/stop", (route) => {
    stops++;
    return route.fulfill({ json: { version: 1, armed: false } });
  });
  await observedFeed(page);
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9876/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9876");
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  const stop = page.getByRole("button", { name: "STOP ROVER", exact: true });
  await expect(stop).toBeEnabled();
  disconnect();
  await expect.poll(() => connections).toBe(2);
  await expect(stop).toBeEnabled();
  await stop.click();
  await expect.poll(() => stops).toBe(1);
  await expect(
    page.getByRole("button", { name: "Arm rover", exact: true }),
  ).toBeDisabled();
});
