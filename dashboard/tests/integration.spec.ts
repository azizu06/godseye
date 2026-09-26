import {
  simulator,
  panel,
  workspace,
  workspaceAction,
  closeWorkspace,
} from "./helpers";
import { test, expect, type Page } from "@playwright/test";
async function external(page: Page, wsUrl: string, commands = false) {
  await workspaceAction(page, "Connection settings");
  await page.getByRole("button", { name: /External feed Connect/ }).click();
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Telemetry WebSocket").fill(wsUrl);
  if (commands) {
    await page.getByLabel("Backend API base").fill("http://localhost:9876");
  } else await page.getByLabel("Enable REST commands").uncheck();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
}
test("Stop is usable inside dialogs and after scrolling", async ({ page }) => {
  await simulator(page);
  await page.getByRole("button", { name: "Explore", exact: true }).click();
  await page
    .getByRole("button", { name: "Arm simulator", exact: true })
    .click();
  await workspaceAction(page, "Workspace help");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "STOP", exact: true })
    .click();
  await page.getByRole("button", { name: "Close dialog", exact: true }).click();
  await closeWorkspace(page);
  await expect(
    page.getByRole("button", { name: "Arm simulator", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Arm simulator", exact: true })
    .click();
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect(
    page.getByRole("button", { name: "STOP ROVER", exact: true }),
  ).toBeInViewport();
});
test("forward moves immediately without turning, leaves a trail, and stops on release and blur", async ({
  page,
}) => {
  await simulator(page);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await page
    .getByRole("button", { name: "Arm simulator", exact: true })
    .click();
  const before = await page
    .locator("svg.map2d g[transform*=rotate]")
    .getAttribute("transform");
  const rover = page.locator("svg.map2d g[transform*=rotate]");
  const heading = (transform: string | null) =>
    transform?.match(/rotate\(([^)]+)\)/)?.[1];
  const initialHeading = heading(await rover.getAttribute("transform"));
  await page.keyboard.down("ArrowUp");
  await expect(rover).not.toHaveAttribute("transform", before!, {
    timeout: 1500,
  });
  expect(heading(await rover.getAttribute("transform"))).toBe(initialHeading);
  await expect
    .poll(() => page.locator("svg.map2d line").count())
    .toBeGreaterThan(1);
  await page.keyboard.up("ArrowUp");
  await page.waitForTimeout(200);
  const after = await page
    .locator("svg.map2d g[transform*=rotate]")
    .getAttribute("transform");
  await page.waitForTimeout(500);
  expect(
    await page
      .locator("svg.map2d g[transform*=rotate]")
      .getAttribute("transform"),
  ).toBe(after);
  await page.keyboard.down("ArrowUp");
  await page.waitForTimeout(400);
  await page.evaluate(() => window.dispatchEvent(new Event("blur")));
  await page.keyboard.up("ArrowUp");
  await page.waitForTimeout(200);
  const blur = await page
    .locator("svg.map2d g[transform*=rotate]")
    .getAttribute("transform");
  await page.waitForTimeout(400);
  expect(
    await page
      .locator("svg.map2d g[transform*=rotate]")
      .getAttribute("transform"),
  ).toBe(blur);
  expect(await page.locator("svg.map2d line").count()).toBeGreaterThan(1);
  await page.keyboard.down("ArrowDown");
  await page.waitForTimeout(400);
  expect(
    (await rover.getAttribute("transform"))?.match(/translate\(([^)]+)\)/)?.[1],
  ).toBe(blur?.match(/translate\(([^)]+)\)/)?.[1]);
  expect(heading(await rover.getAttribute("transform"))).not.toBe(
    initialHeading,
  );
  await page.keyboard.up("ArrowDown");
});

test("older arm completion cannot override a newer stop", async ({ page }) => {
  let armed = false,
    stopCalls = 0,
    releaseArm: () => void = () => {},
    armStarted: () => void = () => {};
  const started = new Promise<void>((r) => (armStarted = r)),
    gate = new Promise<void>((r) => (releaseArm = r));
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    const update = () => {
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone: "ok",
          car: "ok",
          detector: "ok",
          pose_age_ms: 5,
          mode: "manual",
          armed,
          stop_reason: null,
        }),
      );
      ws.send(
        JSON.stringify({
          version: 1,
          type: "pose",
          position: [0, 0.2, 0],
          yaw_rad: 0,
          tracking: "normal",
        }),
      );
    };
    update();
    const id = setInterval(update, 100);
    ws.onClose(() => clearInterval(id));
  });
  await page.route("http://localhost:9876/**", async (route) => {
    if (route.request().url().endsWith("/arm")) {
      armStarted();
      await gate;
      armed = true;
    }
    if (route.request().url().endsWith("/stop")) {
      stopCalls++;
      armed = false;
    }
    await route.fulfill({ json: { version: 1, ok: true } });
  });
  await simulator(page);
  await external(page, "ws://localhost:9876/live", true);
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await started;
  await page.getByRole("button", { name: "STOP ROVER", exact: true }).click();
  await expect.poll(() => stopCalls).toBe(1);
  releaseArm();
  await expect.poll(() => stopCalls).toBe(2);
  await expect(
    page.getByRole("button", { name: "Arm rover", exact: true }),
  ).toBeVisible();
  expect(armed).toBe(false);
});
test("old source arm completion reasserts stop after switching to simulator", async ({
  page,
}) => {
  let armed = false,
    stopCalls = 0,
    releaseArm: () => void = () => {},
    armStarted: () => void = () => {};
  const started = new Promise<void>((r) => (armStarted = r)),
    gate = new Promise<void>((r) => (releaseArm = r));
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    const update = () => {
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone: "ok",
          car: "ok",
          detector: "ok",
          pose_age_ms: 5,
          mode: "manual",
          armed,
          stop_reason: null,
        }),
      );
      ws.send(
        JSON.stringify({
          version: 1,
          type: "pose",
          position: [0, 0.2, 0],
          yaw_rad: 0,
          tracking: "normal",
        }),
      );
    };
    update();
    const id = setInterval(update, 100);
    ws.onClose(() => clearInterval(id));
  });
  await page.route("http://localhost:9876/**", async (route) => {
    if (route.request().url().endsWith("/arm")) {
      armStarted();
      await gate;
      armed = true;
    }
    if (route.request().url().endsWith("/stop")) {
      stopCalls++;
      armed = false;
    }
    await route.fulfill({ json: { version: 1, ok: true } });
  });
  await simulator(page);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await external(page, "ws://localhost:9876/live", true);
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await started;
  await page.getByRole("button", { name: "STOP ROVER", exact: true }).click();
  await expect.poll(() => stopCalls).toBe(1);
  await workspaceAction(page, "Connection settings");
  await page
    .getByRole("button", { name: /Local simulator A complete/ })
    .click();
  await page
    .getByRole("button", { name: "Start simulator", exact: true })
    .click();
  await closeWorkspace(page);
  releaseArm();
  await panel(page, "Spatial memory");
  await expect(
    page.locator(".objects-panel .object-row").first(),
  ).toBeVisible();
  await expect.poll(() => stopCalls).toBe(2);
  await expect(
    page.getByRole("button", { name: "Arm simulator", exact: true }),
  ).toBeVisible();
  expect(armed).toBe(false);
});
test("Python fake-live source renders relocation without simulator geometry", async ({
  page,
}) => {
  test.skip(
    !process.env.GODSEYE_INTEGRATION,
    "Set GODSEYE_INTEGRATION=1 and start tools/fake_live.py on port 8766.",
  );
  await simulator(page);
  await external(page, "ws://127.0.0.1:8766/live");
  await workspace(page);
  await expect(page.getByText("Synthetic feed", { exact: true })).toBeVisible();
  await panel(page, "Spatial memory");
  await expect(page.locator(".objects-panel .object-row")).toHaveCount(1);
  await page.locator(".objects-panel .object-row").click();
  await expect(
    page.getByText("synthetic-backpack", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Relocation detected", { exact: true }),
  ).toBeVisible({ timeout: 15000 });
  await expect(page.locator(".displacement")).toContainText("1.00");
  await expect(
    page.getByRole("button", { name: "Arm rover", exact: true }),
  ).toBeDisabled();
  await panel(page, "Scene settings");
  const points = Number(
    (await page.locator(".scene-stat>strong").innerText()).replaceAll(",", ""),
  );
  expect(points).toBeGreaterThan(0);
  expect(points).toBeLessThan(1000);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  expect(await page.locator(".map2d rect").count()).toBeGreaterThan(1);
  expect(await page.locator(".map2d polyline").count()).toBe(1);
});

test("real backend remains disarmed and rejects a baseline before observations", async ({
  page,
}) => {
  test.skip(
    !process.env.GODSEYE_INTEGRATION,
    "Start the real backend on port 8765 for integration checks.",
  );
  await simulator(page);
  // Point this check at an isolated empty backend, never a phone's active map:
  // GODSEYE_INTEGRATION=1 GODSEYE_TEST_BACKEND_URL=http://127.0.0.1:8767 npm run test:e2e
  const backend =
    process.env.GODSEYE_TEST_BACKEND_URL ?? "http://127.0.0.1:8765";
  const socket = new URL("/live", backend);
  socket.protocol = socket.protocol === "https:" ? "wss:" : "ws:";
  await external(page, socket.toString());
  // Configure the real REST endpoint without replacing its health with fixtures.
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Backend API base").fill(backend);
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await workspace(page);
  await expect(page.getByText(/Receiving telemetry/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Arm rover", exact: true }),
  ).toBeDisabled();
  await panel(page, "Spatial memory");
  await expect(page.locator(".objects-panel .object-row")).toHaveCount(0);
  await panel(page, "Rover controls");
  await page.getByRole("button", { name: "Start rescan", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(
    /No active map|Nothing observed/i,
  );
  await workspaceAction(page, "Workspace help");
  await page
    .locator("dialog")
    .getByRole("button", { name: "STOP", exact: true })
    .click();
  await expect(page.getByRole("status")).toContainText("Stop acknowledged");
});

test("late session response cannot clear a newly selected simulator", async ({
  page,
}) => {
  let release: () => void = () => {},
    started: () => void = () => {};
  const gate = new Promise<void>((r) => (release = r)),
    requestStarted = new Promise<void>((r) => (started = r));
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    const send = () =>
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
        }),
      );
    send();
    const id = setInterval(send, 200);
    ws.onClose(() => clearInterval(id));
  });
  await page.route("http://localhost:9876/session", async (route) => {
    started();
    await gate;
    await route.fulfill({ json: { version: 1 } });
  });
  await simulator(page);
  await external(page, "ws://localhost:9876/live", true);
  await workspaceAction(page, "New session");
  await page
    .getByRole("button", { name: "Start new session", exact: true })
    .click();
  await requestStarted;
  await page.getByRole("button", { name: "Close dialog", exact: true }).click();
  await workspaceAction(page, "Connection settings");
  await page
    .getByRole("button", { name: /Local simulator A complete/ })
    .click();
  await page
    .getByRole("button", { name: "Start simulator", exact: true })
    .click();
  await closeWorkspace(page);
  await panel(page, "Spatial memory");
  await expect(
    page.locator(".objects-panel .object-row").first(),
  ).toBeVisible();
  release();
  await page.waitForTimeout(400);
  await panel(page, "Scene settings");
  await expect
    .poll(async () =>
      Number(
        (await page.locator(".scene-stat>strong").innerText()).replaceAll(
          ",",
          "",
        ),
      ),
    )
    .toBeGreaterThan(0);
});

test("older arm completion cannot override a live map reset", async ({
  page,
}) => {
  let resetMap: () => void = () => {};
  let armed = false,
    stopCalls = 0,
    releaseArm: () => void = () => {},
    armStarted: () => void = () => {};
  const started = new Promise<void>((r) => (armStarted = r)),
    gate = new Promise<void>((r) => (releaseArm = r));
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    const update = () => {
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone: "ok",
          car: "ok",
          detector: "ok",
          pose_age_ms: 5,
          mode: "manual",
          armed,
          stop_reason: null,
        }),
      );
      ws.send(
        JSON.stringify({
          version: 1,
          type: "pose",
          position: [0, 0.2, 0],
          yaw_rad: 0,
          tracking: "normal",
        }),
      );
    };
    resetMap = () =>
      ws.send(
        JSON.stringify({
          version: 1,
          type: "objects",
          session_id: "new-room",
          map_epoch: 2,
          objects: [],
        }),
      );
    ws.send(
      JSON.stringify({
        version: 1,
        type: "objects",
        session_id: "old-room",
        map_epoch: 1,
        objects: [],
      }),
    );
    update();
    const id = setInterval(update, 100);
    ws.onClose(() => clearInterval(id));
  });
  await page.route("http://localhost:9876/**", async (route) => {
    if (route.request().url().endsWith("/arm")) {
      armStarted();
      await gate;
      armed = true;
    }
    if (route.request().url().endsWith("/stop")) {
      stopCalls++;
      armed = false;
    }
    await route.fulfill({ json: { version: 1, ok: true } });
  });
  await simulator(page);
  await external(page, "ws://localhost:9876/live", true);
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await started;
  resetMap();
  await page.waitForTimeout(100);
  releaseArm();
  await expect.poll(() => stopCalls).toBe(1);
  await expect(
    page.getByRole("button", { name: "Arm rover", exact: true }),
  ).toBeVisible();
  expect(armed).toBe(false);
});
