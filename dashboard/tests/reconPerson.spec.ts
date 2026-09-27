import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";
import { closeWorkspace, panel } from "./helpers";

async function recon(page: Page, withEntry = true, delayed = false) {
  const now = Date.now();
  await page.clock.setFixedTime(new Date(now));
  if (delayed)
    await page.addInitScript(() => {
      const fetch = window.fetch.bind(window);
      window.fetch = (input, init) =>
        String(input).endsWith("/route")
          ? fetch(input, { ...init, signal: undefined })
          : fetch(input, init);
    });
  let socket: WebSocketRoute;
  let reconnecting = false;
  let openings = 0;
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  const scope = { session_id: "recon-room", map_epoch: 1 };
  const entry = {
    ...scope,
    start: [-1, -1],
    frame_id: 1,
    t_capture: 10,
    started_at_ms: now - 1000,
    basis: "explore_start",
  };
  let send = (_message: unknown) => {};
  let missionEntry: unknown = withEntry ? entry : null;
  let available = true;
  const bodies: { object_id: string; start: number[] }[] = [];
  const actions: string[] = [];
  const person = (id: string, observations = 3, state = "present") => ({
    id,
    class: "person",
    position: id === "person-1" ? [1, 0.2, 1] : [1.5, 0.2, 0],
    confidence: 0.9,
    observations,
    state,
    first_seen: 100,
    last_seen: now / 1000,
  });
  const objects = (people: ReturnType<typeof person>[]) =>
    send({ version: 1, type: "objects", ...scope, objects: people });
  const occupancy = () =>
    send({
      version: 1,
      type: "occupancy",
      ...scope,
      origin: [-2, -2],
      cell_m: 0.1,
      width: 50,
      height: 50,
      cells: Buffer.alloc(2500, 1).toString("base64"),
    });
  const health = () =>
    send({
      version: 1,
      type: "health",
      phone: "ok",
      car: "ok",
      detector: "ok",
      pose_age_ms: 10,
      mode: "explore",
      armed: true,
      stop_reason: null,
      mission_entry: missionEntry,
    });
  await page.routeWebSocket("ws://localhost:9886/live", (ws) => {
    socket = ws;
    openings++;
    send = (value) => ws.send(JSON.stringify(value));
    health();
    if (!reconnecting) {
      objects([person("person-1", 1)]);
      occupancy();
    }
    send({
      version: 1,
      type: "pose",
      position: [2, 0.2, 2],
      yaw_rad: 0,
      tracking: "normal",
    });
    const timer = setInterval(health, 100);
    ws.onClose(() => clearInterval(timer));
  });
  await page.route("http://localhost:9886/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST" && path !== "/route")
      actions.push(path);
    if (path !== "/route") return route.fulfill({ status: 404 });
    const body = route.request().postDataJSON();
    bodies.push(body);
    if (delayed) await held;
    await route.fulfill({
      json: {
        version: 1,
        ...scope,
        ...body,
        person: [1, 1],
        occupancy_revision: 1,
        ...(available
          ? {
              status: "ok",
              points: [body.start, [0, -1], [0.5, 0.6]],
              approach: [0.5, 0.6],
              length_m: 2.7,
            }
          : { status: "unavailable", reason: "no_observed_free_route" }),
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
    "/?live=ws%3A%2F%2Flocalhost%3A9886%2Flive&api=http%3A%2F%2Flocalhost%3A9886",
  );
  await expect(page.locator(".scene-label")).toContainText("Person");
  return {
    now,
    release,
    openings: () => openings,
    disconnect: () => {
      reconnecting = true;
      socket.close();
    },
    person,
    objects,
    occupancy,
    bodies,
    actions,
    setEntry: (value: unknown) => {
      missionEntry = value;
      health();
    },
    entry,
    unavailable: () => {
      available = false;
    },
    available: () => {
      available = true;
    },
    reset: () => {
      scope.map_epoch++;
      missionEntry = null;
      health();
      objects([]);
    },
  };
}

async function expectOverlaysFit(page: Page) {
  const card = await page.getByTestId("route-card").boundingBox();
  const voice = await page
    .getByRole("region", { name: "Ask Scout by voice" })
    .boundingBox();
  expect(card).not.toBeNull();
  expect(voice).not.toBeNull();
  expect(card!.y + card!.height).toBeLessThanOrEqual(voice!.y);
  expect(card!.x).toBeGreaterThanOrEqual(0);
  expect(card!.x + card!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  await expect(
    page.getByRole("button", { name: "Ask Scout", exact: true }),
  ).toBeVisible();
}

test("repeated person evidence automatically shows a fixed-entry approach in 3D and 2D without motion", async ({
  page,
}) => {
  const feed = await recon(page);
  await page.waitForTimeout(300);
  await expect(page.getByTestId("person-found")).toHaveCount(0);
  expect(feed.bodies).toHaveLength(0);
  feed.objects([feed.person("person-1")]);
  await expect(page.getByTestId("person-found")).toContainText("Person found");
  await expect(page.getByTestId("route-approach-label")).toBeVisible();
  expect(feed.bodies).toHaveLength(1);
  expect(feed.bodies[0]).toMatchObject({
    object_id: "person-1",
    start: [-1, -1],
  });
  await expect(page.getByTestId("route-start-label")).toContainText(
    "MISSION ENTRY",
  );
  await expect(page.getByTestId("route-card")).toContainText("unverified");
  await expectOverlaysFit(page);
  await page.screenshot({ path: "/tmp/godseye-person-found-3d.png" });
  feed.objects([feed.person("person-2"), feed.person("person-1")]);
  await expect(page.getByTestId("route-card")).toContainText(
    "2 people observed",
  );
  expect(feed.bodies).toHaveLength(1); // New people/snapshot order do not steal the target.
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await expect(page.getByTestId("approach-route-2d")).toHaveAttribute(
    "points",
    "-1,-1 0,-1 0.5,0.6",
  );
  await expect(page.getByTestId("route-start-label")).toContainText(
    "MISSION ENTRY",
  );
  await expectOverlaysFit(page);
  await page.screenshot({ path: "/tmp/godseye-person-found-2d.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await expectOverlaysFit(page);
  await page.screenshot({ path: "/tmp/godseye-person-found-mobile.png" });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page
    .getByRole("button", { name: "Select Person on map" })
    .first()
    .click();
  await expect.poll(() => feed.bodies.length).toBe(2);
  expect(feed.bodies[1]).toMatchObject({
    object_id: "person-2",
    start: [-1, -1],
  });
  await closeWorkspace(page);
  await page.getByRole("button", { name: "Clear approach route" }).click();
  feed.objects([feed.person("person-1"), feed.person("person-2")]);
  await page.waitForTimeout(300);
  await expect(page.getByTestId("route-card")).toHaveCount(0);
  expect(feed.bodies).toHaveLength(2);
  expect(feed.actions).toEqual([]);
});

test("missing entry, unreachable floor and stale person never produce a fallback trace", async ({
  page,
}) => {
  const feed = await recon(page, false);
  feed.objects([feed.person("person-1")]);
  await expect(page.getByTestId("person-found")).toContainText("Person found");
  await expect(page.getByTestId("route-status")).toContainText("mission entry");
  expect(feed.bodies).toHaveLength(0);
  feed.unavailable();
  feed.setEntry(feed.entry);
  await expect(page.getByTestId("route-status")).toContainText(
    "no observed-free connection",
  );
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  feed.available();
  feed.occupancy();
  await expect(page.getByTestId("route-approach-label")).toBeVisible();
  feed.objects([feed.person("person-1", 3, "not_found_on_rescan")]);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  await expect(page.getByTestId("person-found")).toHaveCount(0);
  await expect(page.getByTestId("route-status")).toContainText("not found");
  feed.reset();
  await expect(page.getByTestId("route-card")).toHaveCount(0);
  expect(feed.actions).toEqual([]);
});

test("explicit manual approach selection is preserved when another person arrives", async ({
  page,
}) => {
  const feed = await recon(page);
  feed.objects([feed.person("person-1")]);
  await expect(page.getByTestId("route-approach-label")).toBeVisible();
  await panel(page, "Spatial memory");
  await page.locator(".objects-panel .object-row").first().click();
  await page.getByRole("button", { name: "Suggest approach route" }).click();
  await expect(page.getByTestId("route-card")).toContainText(
    "Click the entrance or start point",
  );
  feed.objects([feed.person("person-1"), feed.person("person-2")]);
  await page.waitForTimeout(300);
  await expect(page.getByTestId("route-card")).toContainText(
    "Click the entrance or start point",
  );
  expect(feed.bodies).toHaveLength(1);
  await page
    .locator(".scene-canvas canvas")
    .click({ position: { x: 900, y: 800 } });
  await expect(page.getByTestId("route-approach-label")).toBeVisible();
  expect(feed.bodies).toHaveLength(2);
  expect(feed.bodies[1].start).not.toEqual([-1, -1]);
  await page.getByRole("button", { name: "Clear approach route" }).click();
  feed.objects([
    feed.person("person-1"),
    feed.person("person-2"),
    feed.person("person-3"),
  ]);
  await page.waitForTimeout(300);
  await expect(page.getByTestId("route-card")).toHaveCount(0);
  expect(feed.actions).toEqual([]);
});

test("legacy, malformed and other-map entry cannot invent an origin", async ({
  page,
}) => {
  const feed = await recon(page, false);
  feed.objects([feed.person("person-1")]);
  for (const entry of [
    undefined,
    { bad: true },
    { ...feed.entry, map_epoch: 99 },
    { ...feed.entry, session_id: "other" },
  ]) {
    feed.setEntry(entry);
    await expect(page.getByTestId("person-found")).toBeVisible();
    await expect(page.getByTestId("route-status")).toContainText(
      "mission entry was not recorded",
    );
    await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
    expect(feed.bodies).toHaveLength(0);
  }
  feed.setEntry(feed.entry);
  await expect(page.getByTestId("route-approach-label")).toBeVisible();
  expect(feed.bodies[0].start).toEqual([-1, -1]);
});

test("automatic route requires fresh person evidence after reconnect and expires with it", async ({
  page,
}) => {
  const feed = await recon(page);
  feed.objects([feed.person("person-1")]);
  await expect(page.getByTestId("route-approach-label")).toBeVisible();
  const openings = feed.openings();
  feed.disconnect();
  await expect.poll(feed.openings).toBeGreaterThan(openings);
  await expect(page.getByTestId("person-found")).toHaveCount(0);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  feed.occupancy();
  await page.waitForTimeout(200);
  expect(feed.bodies).toHaveLength(1);
  feed.objects([feed.person("person-1")]);
  await expect(page.getByTestId("route-approach-label")).toBeVisible();
  expect(feed.bodies[1].start).toEqual([-1, -1]);
  await page.clock.setFixedTime(new Date(feed.now + 31000));
  await expect(page.getByTestId("person-found")).toHaveCount(0);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  expect(feed.actions).toEqual([]);
});

test("automatic pending route cannot return after map reset even if HTTP ignores abort", async ({
  page,
}) => {
  const feed = await recon(page, true, true);
  feed.objects([feed.person("person-1")]);
  await expect.poll(() => feed.bodies.length).toBe(1);
  feed.reset();
  await expect(page.getByTestId("route-card")).toHaveCount(0);
  feed.release();
  await page.waitForTimeout(300);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  await expect(page.getByTestId("route-card")).toHaveCount(0);
  expect(feed.actions).toEqual([]);
});
