import {
  observedFeed,
  workspace,
  workspaceAction,
  closeWorkspace,
} from "./helpers";
import { test, expect } from "@playwright/test";
test("tracking follows phone health while backend telemetry remains connected", async ({
  page,
}) => {
  let phone = "ok";
  let age: number | null = 10;
  let sendPose = (_tracking: string) => {};
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    const health = () =>
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone,
          car: "down",
          detector: "down",
          pose_age_ms: age,
          mode: "manual",
          armed: false,
          stop_reason: null,
        }),
      );
    sendPose = (tracking) =>
      ws.send(
        JSON.stringify({
          version: 1,
          type: "pose",
          position: [0, 0.2, 0],
          yaw_rad: 0,
          tracking,
        }),
      );
    health();
    sendPose("normal");
    const id = setInterval(health, 100);
    ws.onClose(() => clearInterval(id));
  });
  await observedFeed(page);
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9876/live");
  await page.getByLabel("Enable REST commands").uncheck();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await workspace(page);
  await expect(page.getByText(/Tracking normal/)).toBeVisible();
  phone = "down";
  age = null;
  await expect(page.getByText(/Tracking unavailable/)).toBeVisible();
  await expect(page.getByText(/Receiving telemetry/)).toBeVisible();
  phone = "stale";
  age = 1000;
  await expect(page.getByText(/Tracking normal/)).toHaveCount(0);
  phone = "ok";
  age = 1000;
  await page.waitForTimeout(200);
  await expect(page.getByText(/Tracking normal/)).toHaveCount(0);
  age = 10;
  sendPose("limited");
  await page.waitForTimeout(200);
  await expect(page.getByText(/Tracking normal/)).toHaveCount(0);
  sendPose("normal");
  await expect(page.getByText(/Tracking normal/)).toBeVisible();
});
