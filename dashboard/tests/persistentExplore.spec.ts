import { test, expect, type Page } from "@playwright/test";
import {
  observedFeed,
  workspaceAction,
  closeWorkspace,
  panel,
} from "./helpers";

async function exploreBackend(
  page: Page,
  options: {
    delayConfirmation?: boolean;
    unavailableConfirmation?: "network" | "HTTP 503";
    delayedArm?: boolean;
    armSuccess?: boolean;
  } = {},
) {
  const state = {
    armed: false,
    requested: false,
    arms: 0,
    stops: 0,
    checking: false,
    connections: 0,
    release: () => {},
    releaseArm: () => {},
    disconnect: () => {},
    succeedNextArm: false,
  };
  const confirmation = new Promise<void>((resolve) => {
    state.release = resolve;
  });
  const armResponse = new Promise<void>((resolve) => {
    state.releaseArm = resolve;
  });
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    state.connections++;
    state.disconnect = () => ws.close();
    const update = () => {
      ws.send(
        JSON.stringify({
          version: 1,
          type: "objects",
          session_id: `map-${state.connections}`,
          map_epoch: 1,
          objects: [],
        }),
      );
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone: "ok",
          car: "ok",
          detector: "ok",
          pose_age_ms: 0,
          mode: "explore",
          armed: state.armed,
          stop_reason:
            state.requested && !state.armed ? "rover_arm_failed" : null,
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
  await page.route("http://localhost:9876/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/autonomy") {
      const requested = state.requested;
      if (options.unavailableConfirmation === "HTTP 503" && requested)
        return route.fulfill({
          status: 503,
          json: { detail: "Status service unavailable" },
        });
      if (options.unavailableConfirmation === "network" && requested)
        return route.abort("failed");
      if (options.delayConfirmation && requested) {
        state.checking = true;
        await confirmation;
      }
      return route.fulfill({
        json: {
          version: 1,
          adapter: "iphone",
          profile: "prototype",
          mode: "explore",
          ready: true,
          auto_requested: requested,
          blockers: [],
        },
      });
    }
    if (path === "/arm") {
      state.arms++;
      state.requested = true;
      if (options.delayedArm) await armResponse;
      if (options.armSuccess || state.succeedNextArm) {
        state.armed = true;
        return route.fulfill({
          json: { version: 1, armed: true, mode: "explore" },
        });
      }
      return route.fulfill({
        status: 409,
        json: { detail: "Temporary rover feedback gap" },
      });
    }
    if (path === "/stop") {
      state.stops++;
      state.requested = state.armed = false;
      return route.fulfill({
        json: { version: 1, armed: false, mode: "explore" },
      });
    }
    return route.fulfill({ status: 404 });
  });
  await observedFeed(page);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9876/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9876");
  await page
    .getByLabel("Rover pairing key")
    .fill("TEST_KEY_NOT_REAL_01234567890123456789");
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await panel(page, "Rover controls");
  await expect(
    page.getByText("Uncalibrated prototype", { exact: true }),
  ).toBeVisible();
  return state;
}

test("confirmed Explore intent survives a failed arm and still exposes Stop", async ({
  page,
}) => {
  const state = await exploreBackend(page);
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Explore is still on");
  expect(state.arms).toBe(1);
  expect(state.stops).toBe(0);
  expect(state.requested).toBe(true);
  await expect(page.locator(".operator-panel .state-pill").last()).toHaveText(
    "Explore resuming",
  );
  state.armed = true; // The backend recovered its own previously selected mission.
  await expect(page.locator(".operator-panel .state-pill").last()).toHaveText(
    "Armed",
  );
  await page.getByRole("button", { name: "STOP ROVER", exact: true }).click();
  await expect.poll(() => state.stops).toBe(1);
  expect(state.requested).toBe(false);
});

test("an older arm response cannot stop a newer explicitly armed Explore mission", async ({
  page,
}) => {
  const state = await exploreBackend(page, { delayConfirmation: true });
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await expect.poll(() => state.checking).toBe(true);
  await page.getByRole("button", { name: "STOP ROVER", exact: true }).click();
  await expect.poll(() => state.stops).toBe(1);
  state.succeedNextArm = true;
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await expect(page.locator(".operator-panel .state-pill").last()).toHaveText(
    "Armed",
  );
  state.release();
  await page.waitForTimeout(300);
  expect(state.stops).toBe(1);
  expect(state.armed).toBe(true);
  expect(state.requested).toBe(true);
});

for (const unavailableConfirmation of ["network", "HTTP 503"] as const) {
  test(`known prototype Explore stays requested when its status refresh fails: ${unavailableConfirmation}`, async ({
    page,
  }) => {
    const state = await exploreBackend(page, { unavailableConfirmation });
    await page.getByRole("button", { name: "Arm rover", exact: true }).click();
    await expect(page.locator(".toast[role=status]")).toContainText(
      "Explore request is waiting for confirmation",
    );
    expect(state.stops).toBe(0);
    expect(state.requested).toBe(true);
    expect(state.armed).toBe(false);
    await page.getByRole("button", { name: "STOP ROVER", exact: true }).click();
    await expect.poll(() => state.stops).toBe(1);
    expect(state.requested).toBe(false);
  });
}

test("Stop wins over a late Explore intent confirmation", async ({ page }) => {
  const state = await exploreBackend(page, { delayConfirmation: true });
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await expect.poll(() => state.checking).toBe(true);
  await page.getByRole("button", { name: "STOP ROVER", exact: true }).click();
  await expect.poll(() => state.stops).toBeGreaterThan(0);
  state.release();
  await expect.poll(() => state.stops).toBe(2);
  expect(state.requested).toBe(false);
  await expect(page.getByRole("status")).not.toContainText(
    "Explore is still on",
  );
});

for (const armSuccess of [false, true]) {
  test(`telemetry reconnect and map replacement preserve a ${armSuccess ? "successful" : "failed"} Explore arm`, async ({
    page,
  }) => {
    const state = await exploreBackend(page, { delayedArm: true, armSuccess });
    await page.getByRole("button", { name: "Arm rover", exact: true }).click();
    await expect.poll(() => state.arms).toBe(1);
    state.disconnect();
    await expect.poll(() => state.connections).toBe(2);
    state.releaseArm();
    if (armSuccess) {
      await expect(
        page.locator(".operator-panel .state-pill").last(),
      ).toHaveText("Armed");
    } else {
      await expect(page.locator(".toast[role=status]")).toContainText(
        "Explore is still on",
      );
    }
    expect(state.stops).toBe(0);
    expect(state.requested).toBe(true);
    await page.getByRole("button", { name: "STOP ROVER", exact: true }).click();
    await expect.poll(() => state.stops).toBe(1);
    expect(state.requested).toBe(false);
  });
}
