import { expect, test } from "@playwright/test";
import { relayServer, TEST_ROVER_KEY } from "./fixtures/relayServer";
import { workspaceAction, closeWorkspace } from "./helpers";

test("shared dashboard reaches ready voice service without revealing server pairing", async ({
  page,
}) => {
  const relay = await relayServer("dev");
  const errors: string[] = [];
  const scripts: Promise<string>[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.request().resourceType() === "script")
      scripts.push(response.text().catch(() => ""));
  });
  await page.routeWebSocket("**/live", (ws) => ws.close());
  await page.route("http://localhost:8765/**", (route) => route.abort());
  try {
    await page.goto(relay.base);
    await workspaceAction(page, "Connection settings");
    await expect(page.getByLabel("Backend API base")).toHaveValue(relay.base);
    await page.getByLabel("Enable REST commands").check();
    await page
      .getByRole("button", { name: "Connect source", exact: true })
      .click();
    await closeWorkspace(page);
    await expect(
      page.getByRole("button", { name: "Ask Scout", exact: true }),
    ).toBeEnabled();
    await expect(page.getByLabel("Ask Scout by voice")).toContainText(
      "Click to ask Scout",
    );
    expect(relay.received.some((request) => request.url === "/voice")).toBe(
      true,
    );
    expect(relay.received.some((request) => request.method === "POST")).toBe(
      false,
    );
    expect((await Promise.all(scripts)).join("\n")).not.toContain(
      TEST_ROVER_KEY,
    );
    expect(errors).toEqual([]);
    await page.screenshot({ path: "/tmp/godseye-relay-voice-ready.png" });
  } finally {
    await page.goto("about:blank");
    await relay.close();
  }
});
