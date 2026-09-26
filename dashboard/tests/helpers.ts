import type { Page } from "@playwright/test";
export async function panel(
  page: Page,
  name:
    | "Spatial memory"
    | "Object intelligence"
    | "Recent activity"
    | "Rover controls",
) {
  const button = page
    .getByRole("navigation", { name: "Workspace panels" })
    .getByRole("button", { name, exact: true });
  if ((await button.getAttribute("aria-expanded")) !== "true")
    await button.click();
}
