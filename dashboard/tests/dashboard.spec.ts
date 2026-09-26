import { panel } from "./helpers";
import { test, expect } from "@playwright/test";
test("spatial memory demo, Blender tools, controls and export", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Godseye spatial workspace" }),
  ).toBeVisible();
  await expect(
    page.getByText("SIMULATED DATA · NO HARDWARE CONNECTED"),
  ).toBeVisible();
  await expect(page.locator("canvas")).toBeVisible();
  await page.getByRole("button", { name: "View controls help" }).click();
  await expect(page.getByText("Shift + middle", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "View controls help" }).click();
  await page.getByRole("button", { name: "Pan tool" }).click();
  await page.getByRole("button", { name: "Reset view" }).click();
  await panel(page, "Rover controls");
  await page.getByRole("button", { name: "Run relocation demo" }).click();
  await expect(
    page.getByText("Relocation detected", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".displacement")).toContainText("1.60");
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await expect(page.getByLabel("Top-down occupancy map")).toBeVisible();
  await page.getByRole("button", { name: "Arm simulator" }).click();
  await expect(
    page.getByRole("button", { name: "Disarm rover" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "STOP ROVER", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Arm simulator" }),
  ).toBeVisible();
  await panel(page, "Rover controls");
  await page.getByRole("button", { name: "Test tracking loss" }).click();
  await expect(
    page.getByRole("button", { name: "Arm simulator" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Restore tracking" }).click();
  await expect(
    page.getByRole("button", { name: "Arm simulator" }),
  ).toBeEnabled();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export snapshot" }).click();
  expect((await download).suggestedFilename()).toContain("simulator");
  expect(errors).toEqual([]);
});
test("sidebar search, session reset, responsive actions", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Object inventory", exact: true }),
  ).toHaveCount(0);
  await expect(page.locator(".topbar")).toHaveCount(0);
  await expect(
    page
      .locator(".heading-actions")
      .getByRole("button", { name: "STOP ROVER", exact: true }),
  ).toBeVisible();
  await panel(page, "Spatial memory");
  await page
    .getByRole("textbox", { name: "Search objects" })
    .first()
    .fill("chair");
  await expect(page.locator(".objects-panel .object-row")).toHaveCount(1);
  await page.locator(".objects-panel .object-row").click();
  await expect(page.locator(".inspector h2")).toHaveText("Chair");
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await page
    .getByRole("button", { name: "Start new session", exact: true })
    .click();
  await expect(page.locator("dialog")).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Close panel", exact: true }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await expect(
    page.getByRole("button", { name: "STOP ROVER", exact: true }),
  ).toBeVisible();
});
test("connection failure stays external, never silently replaces it with simulation", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByRole("button", { name: "Connection settings", exact: true })
    .click();
  await page.getByRole("button", { name: /External feed Connect/ }).click();
  await page
    .getByLabel("Telemetry WebSocket")
    .fill("ws://127.0.0.1:19999/live");
  await page.getByLabel("Enable REST commands").uncheck();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await expect(page.getByText("Reconnecting…", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Waiting for a view of the world" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Arm rover", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("SIMULATED DATA · NO HARDWARE CONNECTED"),
  ).toHaveCount(0);
});
