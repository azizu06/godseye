import { simulator, panel, workspaceAction, closeWorkspace } from "./helpers";
import { test, expect, type WebSocketRoute } from "@playwright/test";

const identity = { session_id: "retained-room", map_epoch: 1 };
const object = (name: string) => ({
  id: name,
  class: name,
  position: [0, 1, 0],
  confidence: 0.9,
  first_seen: 1,
  last_seen: 2,
  observations: 3,
  state: "present",
});
const send = (ws: WebSocketRoute, message: object) =>
  ws.send(JSON.stringify({ version: 1, ...message }));
const objects = (name: string, scope: object = identity) => ({
  type: "objects",
  ...scope,
  objects: [object(name)],
});

test("reconnect retains the known map until scoped identity confirms or replaces it", async ({
  page,
}) => {
  let count = 0;
  let socket: WebSocketRoute;
  await page.routeWebSocket("ws://localhost:9878/live", (ws) => {
    socket = ws;
    count++;
    if (count === 1) {
      send(ws, objects("book"));
      send(ws, {
        type: "pose",
        position: [1, 1, 1],
        yaw_rad: 0,
        tracking: "normal",
      });
    }
  });
  await page.route("http://localhost:8765/capture/**", (route) =>
    route.fulfill({ status: 404 }),
  );
  await simulator(page);
  await workspaceAction(page, "Connection settings");
  await page.getByRole("button", { name: /External feed Connect/ }).click();
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9878/live");
  await page.getByLabel("Enable REST commands").uncheck();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await panel(page, "Spatial memory");
  await expect(page.locator(".object-row")).toContainText("Book");
  await panel(page, "Scene settings");
  await expect(page.locator(".pose-readout")).toHaveCount(1);
  socket!.close();
  await expect.poll(() => count).toBe(2);
  await panel(page, "Spatial memory");
  await expect(page.locator(".object-row")).toContainText("Book");
  await panel(page, "Scene settings");
  await expect(page.locator(".pose-readout")).toHaveCount(0);
  // An empty restarting backend and unscoped messages cannot replace the map.
  send(socket!, {
    type: "objects",
    session_id: null,
    map_epoch: null,
    objects: [],
  });
  send(socket!, objects("rogue", {}));
  send(socket!, {
    type: "pose",
    position: [99, 0, 99],
    yaw_rad: 0,
    tracking: "normal",
  });
  await page.waitForTimeout(100);
  await panel(page, "Spatial memory");
  await expect(page.locator(".object-row")).toContainText("Book");
  await panel(page, "Scene settings");
  await expect(page.locator(".pose-readout")).toHaveCount(0);
  send(socket!, objects("book"));
  send(socket!, {
    type: "pose",
    position: [2, 1, 2],
    yaw_rad: 0,
    tracking: "normal",
  });
  await panel(page, "Scene settings");
  await expect(page.locator(".pose-readout")).toContainText("2.00");
  send(socket!, objects("cup", { ...identity, map_epoch: 2 }));
  await panel(page, "Spatial memory");
  await expect(page.locator(".object-row")).toContainText("Cup");
  await panel(page, "Spatial memory");
  await expect(page.locator(".object-row")).not.toContainText("Book");
});

test("changing REST permission leaves the map and WebSocket intact", async ({
  page,
}) => {
  let count = 0;
  await page.routeWebSocket("ws://localhost:9878/live", (ws) => {
    count++;
    if (count === 1) send(ws, objects("book"));
  });
  await page.route("http://localhost:8765/**", (route) =>
    route.fulfill({ status: 404 }),
  );
  await simulator(page);
  await workspaceAction(page, "Connection settings");
  await page.getByRole("button", { name: /External feed Connect/ }).click();
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9878/live");
  await page.getByLabel("Enable REST commands").uncheck();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await panel(page, "Spatial memory");
  await expect(page.locator(".object-row")).toContainText("Book");
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Enable REST commands").check();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await page.waitForTimeout(200);
  expect(count).toBe(1);
  await panel(page, "Spatial memory");
  await expect(page.locator(".object-row")).toContainText("Book");
});
