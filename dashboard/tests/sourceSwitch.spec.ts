import { test, expect } from "@playwright/test";
test("source switch stops an armed backend and retains it when Stop fails", async ({
  page,
}) => {
  let fail = true,
    calls = 0;
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    const update = () =>
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
    update();
    const id = setInterval(update, 200);
    ws.onClose(() => clearInterval(id));
  });
  await page.route("http://localhost:9876/stop", (route) => {
    calls++;
    return route.fulfill({
      status: fail ? 500 : 200,
      json: fail ? { detail: "Stop failed" } : { version: 1, armed: false },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Scene layers", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Pan tool", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Connection settings", exact: true })
    .click();
  await page.getByRole("button", { name: /External feed Connect/ }).click();
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9876/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9876");
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await expect(
    page.getByText("Receiving telemetry", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Connection settings", exact: true })
    .click();
  await page
    .getByRole("button", { name: /Local simulator A complete/ })
    .click();
  await page
    .getByRole("button", { name: "Start simulator", exact: true })
    .click();
  await expect(
    page.getByText(/Connection unchanged; retry Stop/),
  ).toBeVisible();
  expect(calls).toBe(1);
  await expect(page.getByRole("dialog")).toBeVisible();
  fail = false;
  await page
    .getByRole("button", { name: "Start simulator", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(calls).toBe(2);
  await expect(
    page.getByText("SIMULATED DATA · NO HARDWARE CONNECTED"),
  ).toBeVisible();
});
