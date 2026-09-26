import { test, expect, type Page, type WebSocketRoute } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { closeWorkspace, panel, workspaceAction } from "./helpers";

test("without an external feed the workspace invents neither data nor control status", async ({
  page,
}) => {
  let commands = 0;
  await page.routeWebSocket("ws://localhost:8765/live", (socket) =>
    socket.close(),
  );
  await page.route("http://localhost:8765/**", (route) => {
    if (route.request().method() === "POST") commands++;
    return route.fulfill({ status: 404, body: "No test feed" });
  });
  await page.goto("/");
  const empty = await snapshot(page);
  expect(empty.source).toBe("external");
  expect(empty.health).toBeNull();
  expect(empty.pose).toBeNull();
  expect(empty.objects).toEqual([]);
  expect(empty.events).toEqual([]);
  expect(empty.point_chunks).toEqual([]);
  expect(empty.occupancy).toBeNull();
  expect(empty.colored_reconstruction).toBeNull();
  expect(empty.trajectory).toEqual([]);
  await expect(
    page.getByRole("button", { name: "Arm rover", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Standard", exact: true }),
  ).toHaveAttribute("aria-pressed", "false");
  await expect(
    page.getByRole("button", { name: "Explore", exact: true }),
  ).toHaveAttribute("aria-pressed", "false");
  await panel(page, "Rover controls");
  await expect(
    page
      .getByRole("dialog", { name: "Rover controls panel" })
      .getByText("Waiting for status", { exact: true }),
  ).toBeVisible();
  await workspaceAction(page, "Connection settings");
  await expect(
    page.getByRole("heading", { name: "Connect your world" }),
  ).toBeVisible();
  await expect(
    page.getByText(/Local simulator|Python fake feed|Start simulator/),
  ).toHaveCount(0);
  await expect(page.getByLabel("Enable REST commands")).not.toBeChecked();
  await page.getByRole("button", { name: "STOP", exact: true }).click();
  await expect(page.getByText(/This feed is telemetry only/)).toBeVisible();
  await page.getByRole("button", { name: "Close dialog" }).click();
  await closeWorkspace(page);
  await expect(
    page.getByRole("button", { name: "Arm rover", exact: true }),
  ).toBeDisabled();
  expect(commands).toBe(0);
  await page.screenshot({ path: "/tmp/godseye-real-empty-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "/tmp/godseye-real-empty-mobile.png" });
});

async function snapshot(page: Page) {
  const download = page.waitForEvent("download");
  await workspaceAction(page, "Export snapshot");
  const result = JSON.parse(
    await readFile((await (await download).path())!, "utf8"),
  );
  await closeWorkspace(page);
  return result;
}

test("only received observations populate the map and disconnect retains them without inventing more", async ({
  page,
}) => {
  let socket: WebSocketRoute | undefined;
  let accept = true;
  await page.routeWebSocket("ws://localhost:8765/live", (ws) => ws.close());
  await page.route("http://localhost:8765/**", (route) =>
    route.fulfill({ status: 404 }),
  );
  await page.routeWebSocket("ws://localhost:9881/live", (ws) => {
    if (accept) socket = ws;
    else ws.close();
  });
  await page.route("http://localhost:9881/**", (route) =>
    route.fulfill({ status: 404 }),
  );
  await page.goto("/");
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9881/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9881");
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await expect.poll(() => Boolean(socket)).toBe(true);
  const scope = { session_id: "received-only", map_epoch: 1 };
  const object = {
    id: "actual-chair",
    class: "chair",
    position: [1, 1, -2],
    confidence: 0.94,
    first_seen: 1,
    last_seen: 2,
    observations: 3,
    state: "present",
  };
  const send = (message: object) =>
    socket!.send(JSON.stringify({ version: 1, ...scope, ...message }));
  send({
    type: "health",
    phone: "ok",
    car: "down",
    detector: "ok",
    pose_age_ms: 0,
    mode: "manual",
    armed: false,
    stop_reason: null,
  });
  send({ type: "pose", position: [0, 1, 0], yaw_rad: 0, tracking: "normal" });
  send({ type: "objects", objects: [object] });
  send({
    type: "points",
    chunk_id: 1,
    positions: [1, 1, -2, 1.1, 1, -2, 1, 1.1, -2],
    colors: [1, 0, 0, 0, 1, 0, 0, 0, 1],
  });
  await panel(page, "Spatial memory");
  await expect(page.locator(".object-row")).toHaveCount(1);
  await expect(page.locator(".object-row")).toContainText("Chair");
  const received = await snapshot(page);
  await page.screenshot({ path: "/tmp/godseye-real-received-desktop.png" });
  expect(received.objects).toEqual([object]);
  expect(received.pose.position).toEqual([0, 1, 0]);
  expect(received.point_chunks).toHaveLength(1);
  expect(received.point_chunks[0].positions).toEqual([
    1, 1, -2, 1.1, 1, -2, 1, 1.1, -2,
  ]);
  expect(received.occupancy).toBeNull();
  expect(received.colored_reconstruction).toBeNull();
  accept = false;
  socket!.close();
  await page.waitForTimeout(1600);
  const offline = await snapshot(page);
  // Failed reconnects preserve the last received snapshot as history. A
  // successful reopen clears transient pose/chunks (missionLifecycle.spec.ts).
  expect(offline.pose).toEqual(received.pose);
  expect(offline.objects).toEqual(received.objects);
  expect(offline.point_chunks).toEqual(received.point_chunks);
  expect(offline.occupancy).toBeNull();
  expect(offline.colored_reconstruction).toBeNull();
  expect(offline.events).toEqual(received.events);
  expect(offline.trajectory).toEqual(received.trajectory);
  await expect(
    page.getByRole("button", { name: "Arm rover", exact: true }),
  ).toBeDisabled();
});
