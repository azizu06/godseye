import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";
import { panel, workspaceAction } from "./helpers";

async function routeFixture(page: Page, delayed = false) {
  if (delayed)
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.fetch = (input, init) =>
        String(input).endsWith("/route")
          ? fetch(input, { ...init, signal: undefined })
          : fetch(input, init);
    });
  const scope = { session_id: "route-room", map_epoch: 1 };
  let socket: WebSocketRoute;
  let openings = 0;
  let reconnecting = false;
  let phone = "ok";
  let heartbeat = true;
  let requests = 0;
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  const person = {
    id: "person-1",
    class: "person",
    position: [1, 0.2, 1],
    confidence: 0.9,
    first_seen: Date.now() / 1000,
    last_seen: Date.now() / 1000,
    observations: 3,
    state: "present",
  };
  const send = (message: unknown) => socket.send(JSON.stringify(message));
  const objects = (state = "present", lastSeen = Date.now() / 1000) =>
    send({
      version: 1,
      type: "objects",
      ...scope,
      objects: [{ ...person, state, last_seen: lastSeen }],
    });
  const occupancy = () =>
    send({
      version: 1,
      type: "occupancy",
      ...scope,
      origin: [-2, -2],
      cell_m: 0.05,
      width: 80,
      height: 80,
      cells: Buffer.alloc(6400, 1).toString("base64"),
    });
  await page.routeWebSocket("ws://localhost:9884/live", (ws) => {
    socket = ws;
    openings++;
    const health = () => {
      if (heartbeat)
        ws.send(
          JSON.stringify({
            version: 1,
            type: "health",
            phone,
            car: "down",
            detector: "ok",
            pose_age_ms: phone === "ok" ? 10 : null,
            mode: "manual",
            armed: false,
            stop_reason: null,
          }),
        );
    };
    health();
    if (!reconnecting) {
      objects();
      occupancy();
    }
    const interval = setInterval(health, 100);
    ws.onClose(() => clearInterval(interval));
  });
  await page.route("http://localhost:9884/**", async (route) => {
    if (new URL(route.request().url()).pathname !== "/route")
      return route.fulfill({ status: 404 });
    requests++;
    const body = route.request().postDataJSON();
    if (delayed) await held;
    await route.fulfill({
      json: {
        version: 1,
        ...scope,
        object_id: body.object_id,
        start: body.start,
        person: [1, 1],
        occupancy_revision: 1,
        status: "ok",
        points: [body.start, [0.6, 0.4], [0.5, 0.6]],
        approach: [0.5, 0.6],
        length_m: 1.4,
        assumptions: {
          walker_radius_m: 0.25,
          margin_m: 0.05,
          person_keep_out_m: 0.3,
          approach_reach_m: 1.5,
          unknown: "blocked",
          doors: "not_inferred",
          verified: false,
        },
      },
    });
  });
  await page.goto(
    "/?live=ws%3A%2F%2Flocalhost%3A9884%2Flive&api=http%3A%2F%2Flocalhost%3A9884",
  );
  await panel(page, "Spatial memory");
  await page.locator(".objects-panel .object-row").first().click();
  await page.getByRole("button", { name: "Suggest approach route" }).click();
  await page
    .locator(".scene-canvas canvas")
    .click({ position: { x: 900, y: 800 } });
  await expect.poll(() => requests).toBe(1);
  if (!delayed)
    await expect(page.getByTestId("route-approach-label")).toBeVisible();
  return {
    objects,
    occupancy,
    release,
    requests: () => requests,
    openings: () => openings,
    disconnect: () => {
      reconnecting = true;
      socket.close();
    },
    phoneDown: () => {
      phone = "down";
    },
    stopHeartbeat: () => {
      heartbeat = false;
    },
    reset: () =>
      send({
        version: 1,
        type: "objects",
        session_id: "route-room",
        map_epoch: 2,
        objects: [],
      }),
  };
}

for (const condition of [
  "not-found",
  "last-seen",
  "disconnect",
  "old-person",
  "phone-down",
  "stale-feed",
] as const) {
  test(`a ready approach route retires on ${condition} while object memory remains`, async ({
    page,
  }) => {
    const feed = await routeFixture(page);
    if (condition === "not-found") feed.objects("not_found_on_rescan");
    else if (condition === "last-seen") feed.objects("last_seen");
    else if (condition === "disconnect") feed.disconnect();
    else if (condition === "phone-down") feed.phoneDown();
    else if (condition === "stale-feed") feed.stopHeartbeat();
    else await page.clock.setFixedTime(new Date(Date.now() + 31_000));
    await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
    await expect(page.getByTestId("route-status")).toContainText(
      "Route unavailable",
    );
    if (condition === "not-found")
      await expect(page.getByTestId("route-status")).toContainText(
        "not found on the latest rescan",
      );
    if (condition === "old-person")
      await expect(page.getByTestId("route-status")).toContainText(
        "last observation is out of date",
      );
    await panel(page, "Spatial memory");
    await expect(page.locator(".objects-panel .object-row")).toHaveCount(1);
    expect(feed.requests()).toBe(1);
    if (condition === "old-person") {
      feed.objects("present", await page.evaluate(() => Date.now() / 1000));
      await expect(page.getByTestId("route-approach-label")).toBeVisible();
      expect(feed.requests()).toBe(2);
    }
  });
}

for (const condition of ["not-found", "disconnect"] as const) {
  test(`a pending route cannot publish its late result after ${condition}`, async ({
    page,
  }) => {
    const feed = await routeFixture(page, true);
    if (condition === "not-found") feed.objects("not_found_on_rescan");
    else feed.disconnect();
    await expect(page.getByTestId("route-status")).toContainText(
      "Route unavailable",
    );
    feed.release();
    await page.waitForTimeout(300);
    await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
    await expect(page.getByTestId("route-status")).toContainText(
      "Route unavailable",
    );
  });
}

test("same-map reconnect requires both map identity and a new person snapshot before rechecking", async ({
  page,
}) => {
  const feed = await routeFixture(page);
  const openings = feed.openings();
  feed.disconnect();
  await expect.poll(feed.openings).toBeGreaterThan(openings);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  expect(feed.requests()).toBe(1);
  feed.occupancy(); // Confirms map identity, but the person is still retained history.
  await page.waitForTimeout(300);
  expect(feed.requests()).toBe(1);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  feed.objects();
  await expect(page.getByTestId("route-approach-label")).toBeVisible();
  expect(feed.requests()).toBe(2);
});

for (const condition of ["map-reset", "source-change"] as const) {
  test(`a pending route is cleared across ${condition}, including an abort-ignoring reply`, async ({
    page,
  }) => {
    const feed = await routeFixture(page, true);
    if (condition === "map-reset") feed.reset();
    else {
      await page.routeWebSocket("ws://localhost:9885/live", () => {});
      await page.route("http://localhost:9885/**", (route) =>
        route.fulfill({ status: 404 }),
      );
      await workspaceAction(page, "Connection settings");
      await page
        .getByLabel("Telemetry WebSocket")
        .fill("ws://localhost:9885/live");
      await page.getByLabel("Backend API base").fill("http://localhost:9885");
      await page
        .getByRole("button", { name: "Connect source", exact: true })
        .click();
    }
    await expect(page.getByTestId("route-card")).toHaveCount(0);
    feed.release();
    await page.waitForTimeout(300);
    await expect(page.getByTestId("route-card")).toHaveCount(0);
    await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  });
}
