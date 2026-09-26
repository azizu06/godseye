import {
  simulator,
  panel,
  workspace,
  workspaceAction,
  closeWorkspace,
} from "./helpers";
import { test, expect } from "@playwright/test";
test("spatial memory demo, Blender tools, controls and export", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await simulator(page);
  await expect(
    page.getByRole("heading", { name: "Godseye spatial workspace" }),
  ).toBeVisible();
  await workspace(page);
  await expect(
    page.getByText("Virtual scene · no hardware connected"),
  ).toBeVisible();
  await panel(page, "Scene settings");
  await expect(page.locator("canvas")).toBeVisible();
  await page.getByRole("button", { name: "View controls help" }).click();
  await expect(page.getByText("Shift + middle", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "View controls help" }).click();
  await page.getByRole("button", { name: "Reset view" }).click();
  await panel(page, "Rover controls");
  await page.getByRole("button", { name: "Run relocation demo" }).click();
  await expect(
    page.getByText("Relocation detected", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".displacement")).toContainText("1.60");
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await expect(page.getByLabel("Top-down occupancy map")).toBeVisible();
  await closeWorkspace(page);
  await page.getByRole("button", { name: "Arm simulator" }).click();
  await expect(
    page.getByRole("button", { name: "STOP ROVER", exact: true }),
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
  await workspaceAction(page, "Export snapshot");
  expect((await download).suggestedFilename()).toContain("simulator");
  expect(errors).toEqual([]);
});
test("sidebar search, session reset, responsive actions", async ({ page }) => {
  await simulator(page);
  await expect(
    page.getByRole("button", { name: "Object inventory", exact: true }),
  ).toHaveCount(0);
  await expect(page.locator(".topbar")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Arm simulator", exact: true }),
  ).toBeVisible();
  await panel(page, "Spatial memory");
  await page
    .getByRole("textbox", { name: "Search objects" })
    .first()
    .fill("chair");
  await expect(page.locator(".objects-panel .object-row")).toHaveCount(1);
  await page.locator(".objects-panel .object-row").click();
  await expect(page.locator(".inspector h2")).toHaveText("Chair");
  await workspaceAction(page, "New session");
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
    page.getByRole("button", { name: "Arm simulator", exact: true }),
  ).toBeVisible();
});
test("connection failure stays external, never silently replaces it with simulation", async ({
  page,
}) => {
  await simulator(page);
  await workspaceAction(page, "Connection settings");
  await page.getByRole("button", { name: /External feed Connect/ }).click();
  await page.getByLabel("Enable REST commands").check();
  await page
    .getByLabel("Telemetry WebSocket")
    .fill("ws://127.0.0.1:19999/live");
  await page.getByLabel("Enable REST commands").uncheck();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await workspace(page);
  await expect(page.getByText(/Reconnecting…/)).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Waiting for a view of the world" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Arm rover", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("Virtual scene · no hardware connected"),
  ).toHaveCount(0);
});
