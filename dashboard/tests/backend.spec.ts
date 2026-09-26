import { panel } from "./helpers";
import { test, expect } from "@playwright/test";
test("scoped history, live overlap, map reset and rescan acknowledgement", async ({
  page,
}) => {
  let releaseRescan: () => void = () => {};
  let rescanCalls = 0;
  const rescanGate = new Promise<void>((r) => (releaseRescan = r));
  let send: (value: unknown) => void = () => {};
  const scope = { session_id: "room", map_epoch: 1 };
  const object = {
    id: "old",
    class: "backpack",
    position: [1, 0.4, 2],
    confidence: 0.9,
    first_seen: 1,
    last_seen: 2,
    observations: 5,
    state: "last_seen",
  };
  const event = {
    id: "event-7",
    rescan_id: "scan-2",
    kind: "possible_move",
    object_id: "old",
    new_object_id: "candidate",
    old_position: [1, 0.4, 2],
    new_position: [2, 0.4, 2],
    displacement_m: 1,
    t: 10,
  };
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    send = (value) => ws.send(JSON.stringify(value));
    const health = () =>
      send({
        version: 1,
        type: "health",
        phone: "ok",
        car: "down",
        detector: "ok",
        pose_age_ms: 5,
        mode: "manual",
        armed: false,
        stop_reason: null,
      });
    health();
    const interval = setInterval(health, 200);
    ws.onClose(() => clearInterval(interval));
    send({ version: 1, type: "objects", ...scope, objects: [object] });
    send({
      version: 1,
      type: "points",
      ...scope,
      chunk_id: 1,
      positions: [1, 0, 2],
      colors: [0.5, 0.5, 0.5],
    });
    send({ version: 1, type: "event", ...scope, ...event });
  });
  await page.route("http://localhost:9876/events", (route) =>
    route.fulfill({ json: { version: 1, ...scope, events: [event] } }),
  );
  await page.route("http://localhost:9876/rescan", async (route) => {
    rescanCalls++;
    if (rescanCalls === 2) await rescanGate;
    await route.fulfill({
      json: { version: 1, ...scope, rescan_id: "scan-3", baseline_objects: 1 },
    });
  });
  await page.goto("/");
  await page
    .getByRole("button", { name: "Connection settings", exact: true })
    .click();
  await page.getByRole("button", { name: /External feed Connect/ }).click();
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9876/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9876");
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await panel(page, "Recent activity");
  await expect(
    page.getByRole("heading", { name: "Recent activity" }),
  ).toHaveAttribute("title", "Saved + live history");
  await expect(page.locator(".activity-panel .count-badge")).toHaveText("1");
  await expect(page.getByText("Same object. A new chapter.")).toHaveCount(0);
  await panel(page, "Spatial memory");
  await page.locator(".objects-panel .object-row").first().click();
  await expect(
    page.getByText("Possible relocation", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Candidate identity: candidate")).toBeVisible();
  await panel(page, "Spatial memory");
  await page.locator(".objects-panel .object-row").last().click();
  await expect(page.locator(".old-marker")).toHaveCount(1);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await expect(page.locator("svg.map2d line")).toHaveCount(1);
  await panel(page, "Rover controls");
  await page.getByRole("button", { name: "Start rescan", exact: true }).click();
  await expect(
    page.getByText("Baseline saved · 1 objects · watching observations"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Start rescan", exact: true }),
  ).toBeEnabled();
  await panel(page, "Rover controls");
  await page.getByRole("button", { name: "Start rescan", exact: true }).click();
  await expect.poll(() => rescanCalls).toBe(2);
  send({
    version: 1,
    type: "objects",
    session_id: "new-room",
    map_epoch: 1,
    objects: [],
  });
  releaseRescan();
  await panel(page, "Spatial memory");
  await expect(page.locator(".objects-panel .object-row")).toHaveCount(0);
  await expect(
    page.getByText("Baseline saved · 1 objects · watching observations"),
  ).toHaveCount(0);
  await expect(page.locator(".scene-stat>strong")).toHaveText("0");
  await panel(page, "Recent activity");
  await expect(page.locator(".activity-panel .count-badge")).toHaveText("0");
  await expect(
    page.getByRole("heading", { name: "Recent activity" }),
  ).toHaveAttribute("title", "Saved history unavailable · live events only");
});
