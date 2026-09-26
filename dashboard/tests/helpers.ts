import type { Page } from "@playwright/test";

export async function workspace(page: Page) {
  if (await page.getByRole("navigation", { name: "Workspace panels" }).count())
    return;
  const back = page.getByRole("button", {
    name: "Workspace menu",
    exact: true,
  });
  if (await back.count()) await back.click();
  else {
    // The launcher is intentionally visible only while keyboard-focused.
    const launcher = page.getByRole("button", {
      name: "Open workspace",
      exact: true,
    });
    await launcher.focus();
    await launcher.press("Enter");
  }
}

export async function closeWorkspace(page: Page) {
  const close = page.getByRole("button", { name: "Close panel", exact: true });
  if (await close.count()) await close.click();
}

export async function workspaceAction(page: Page, name: string) {
  await workspace(page);
  await page.getByRole("button", { name, exact: true }).click();
}

export async function panel(
  page: Page,
  name:
    | "Spatial memory"
    | "Object intelligence"
    | "Recent activity"
    | "Rover controls"
    | "Scene settings",
) {
  if (
    await page
      .getByRole("dialog", { name: `${name} panel`, exact: true })
      .count()
  )
    return;
  await workspace(page);
  await page
    .getByRole("navigation", { name: "Workspace panels" })
    .getByRole("button", { name, exact: true })
    .click();
}

// Demo tests select their source through the same settings UI as an operator;
// production may safely default to an external, telemetry-only connection.
export async function simulator(page: Page) {
  await page.goto("/");
  await workspaceAction(page, "Connection settings");
  await page
    .getByRole("button", { name: /Local simulator A complete/ })
    .click();
  await page
    .getByRole("button", { name: "Start simulator", exact: true })
    .click();
  await closeWorkspace(page);
  await page.getByRole("button", { name: "3D", exact: true }).click();
}
