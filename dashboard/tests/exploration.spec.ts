import { test, expect, type WebSocketRoute } from "@playwright/test";
import { panel } from "./helpers";

test("Explore shows scoped backend phases and honest partial completion without sending commands", async ({
  page,
}) => {
  let socket: WebSocketRoute | undefined;
  let posts = 0;
  await page.routeWebSocket("ws://localhost:8765/live", (ws) => {
    socket = ws;
  });
  await page.route("http://localhost:8765/**", (route) => {
    if (route.request().method() === "POST") posts++;
    return route.fulfill({ status: 404 });
  });
  await page.goto("/");
  await expect.poll(() => Boolean(socket)).toBe(true);
  const scope = { session_id: "scan-test", map_epoch: 1 };
  const status = {
    phase: "selecting",
    reason: null,
    ...scope,
    observed_views: 4,
    gained_cells: 21,
    last_gain_cells: 3,
    gained_surface_voxels: 98,
    last_gain_surface_voxels: 12,
    target: [1, 2],
  };
  const send = (patch: object = {}, extra: object = {}) =>
    socket!.send(
      JSON.stringify({
        version: 1,
        type: "health",
        ...scope,
        phone: "ok",
        car: "down",
        detector: "ok",
        pose_age_ms: 0,
        mode: "explore",
        armed: true,
        stop_reason: null,
        exploration: { ...status, ...patch },
        ...extra,
      }),
    );
  send();
  await panel(page, "Rover controls");
  const compact = page
    .getByRole("dialog", { name: "Rover controls panel" })
    .getByLabel("Explore scan status");
  for (const phase of [
    "selecting",
    "moving",
    "aligning",
    "settling",
    "scanning",
  ]) {
    send({ phase });
    await expect(compact).toContainText(`Explore · ${phase}`);
  }
  await panel(page, "Rover controls");
  const detail = page
    .getByRole("dialog", { name: "Rover controls panel" })
    .getByLabel("Explore scan status");
  send({ phase: "scanning" });
  await expect(
    page.getByText("Enable REST commands to control exploration."),
  ).toBeVisible();
  await expect(
    page.getByText("Exploration requires backend support", { exact: true }),
  ).toHaveCount(0);
  await expect(detail).toContainText("4 stable views · 98 new surface voxels");
  send({ phase: "blocked", reason: "scan_budget" }, { armed: false });
  await expect(detail).toContainText("Partial scan · blocked");
  await expect(detail).toContainText("configured limit");
  send(
    { phase: "complete", reason: "scan_accessible_exhausted" },
    { armed: false },
  );
  await expect(detail).toContainText("Accessible scan done");
  await expect(detail).toContainText("Unseen areas may remain");
  send(
    { phase: "complete", reason: "scan_diminishing_returns" },
    { armed: false },
  );
  await expect(detail).toContainText("Scan settled");
  await expect(detail).toContainText(
    "Recent reachable views added little new detail. Unseen areas may remain.",
  );
  await expect(detail).not.toContainText("exhausted");
  send({ phase: "blocked", reason: "scan_search_limit" }, { armed: false });
  await expect(detail).toContainText("Partial scan · blocked");
  send({}, { exploration: undefined });
  await expect(detail).toContainText("Explore status unavailable");
  send({ phase: "moving" }, { session_id: "new-session", map_epoch: 2 });
  await expect(detail).toContainText("Waiting for scan status");
  send({ phase: "moving", map_epoch: 0 });
  await expect(detail).toContainText("Waiting for scan status");
  send({ phase: "moving" }, { armed: false, stop_reason: "operator_stop" });
  await expect(detail).toContainText("Explore stopped");
  send({ phase: "scanning" });
  await expect(detail).toContainText("Explore status stale", { timeout: 6000 });
  send({ phase: "scanning" });
  await expect(compact).toContainText("Explore · scanning");
  await page.screenshot({ path: "/tmp/godseye-explore-status-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  send({ phase: "scanning" });
  await page.screenshot({ path: "/tmp/godseye-explore-status-mobile.png" });
  const bounds = await compact.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  expect(posts).toBe(0);
});
