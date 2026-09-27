import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";
import { panel, workspaceAction } from "./helpers";

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
  const unavailableIds = new Set<string>();
  let active = 0,
    maximumActive = 0;
  const bodies: { object_id: string; start: number[]; purpose?: string }[] = [];
  const actions: string[] = [];
  const person = (id: string, observations = 3, state = "present") => ({
    id,
    class: "person",
    position: [
      [1, 0.2, 1],
      [1.5, 0.2, 0],
      [-0.5, 0.2, 1.5],
      [2, 0.2, 1],
      [-1, 0.2, 2],
    ][Number(id.split("-")[1]) - 1] ?? [0, 0.2, 0],
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
    active++;
    maximumActive = Math.max(maximumActive, active);
    if (delayed) await held;
    await new Promise((resolve) => setTimeout(resolve, 100));
    const position = person(body.object_id).position;
    const endpoint = [position[0] - 0.3, position[2] - 0.3];
    await route.fulfill({
      json: {
        version: 1,
        ...scope,
        ...body,
        person: [1, 1],
        occupancy_revision: 1,
        ...(available && !unavailableIds.has(body.object_id)
          ? {
              status: "ok",
              points: [body.start, [0, -1], endpoint],
              approach: endpoint,
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
    active--;
  });
  await page.goto(
    "/?live=ws%3A%2F%2Flocalhost%3A9886%2Flive&api=http%3A%2F%2Flocalhost%3A9886",
  );
  await expect(page.locator(".scene-label")).toContainText("Person");
  return {
    now,
    send,
    scope,
    unavailableIds,
    maximumActive: () => maximumActive,
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

test("all five people receive independent queued approaches in both views, including one unavailable branch", async ({
  page,
}) => {
  const feed = await recon(page);
  feed.unavailableIds.add("person-5");
  await expect(page.getByTestId("person-found")).toHaveCount(0);
  expect(feed.bodies).toHaveLength(0);
  const people = [1, 2, 3, 4, 5].map((n) => feed.person(`person-${n}`));
  feed.objects(people);
  await expect(page.getByTestId("person-found")).toHaveText("5 people found");
  await expect(page.getByTestId("route-approach-label")).toHaveCount(4);
  await expect(page.getByTestId("recon-summary")).toContainText(
    "4 suggested approaches · 1 unavailable",
  );
  expect(feed.bodies).toHaveLength(5);
  expect(feed.maximumActive()).toBeLessThanOrEqual(2);
  expect(new Set(feed.bodies.map((body) => body.object_id)).size).toBe(5);
  for (const body of feed.bodies)
    expect(body).toMatchObject({ start: [-1, -1], purpose: "recon" });
  await expectOverlaysFit(page);
  await page.screenshot({ path: "/tmp/godseye-people-found-3d.png" });
  feed.objects([...people].reverse());
  feed.occupancy();
  await page.waitForTimeout(250);
  expect(feed.bodies).toHaveLength(5);
  await page
    .getByTestId("recon-person")
    .filter({ hasText: "Person 2" })
    .click();
  await expect(page.getByTestId("route-approach-label")).toHaveCount(4);
  expect(feed.bodies).toHaveLength(5);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await expect(page.getByTestId("approach-route-2d")).toHaveCount(4);
  for (const line of await page.getByTestId("approach-route-2d").all())
    expect(await line.getAttribute("points")).toMatch(/^-1,-1 0,-1 /);
  expect(
    new Set(
      await page
        .getByTestId("approach-route-2d")
        .evaluateAll((lines) =>
          lines.map((line) => line.getAttribute("points")),
        ),
    ).size,
  ).toBe(4);
  await expectOverlaysFit(page);
  await page.screenshot({ path: "/tmp/godseye-people-found-2d.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  await expectOverlaysFit(page);
  await page.screenshot({ path: "/tmp/godseye-people-found-mobile.png" });
  feed.objects(
    people.map((person) =>
      person.id === "person-2"
        ? { ...person, state: "not_found_on_rescan" }
        : person,
    ),
  );
  await expect(page.getByTestId("approach-route-2d")).toHaveCount(3);
  await expect(
    page.getByTestId("recon-person").filter({ hasText: "Person 2" }),
  ).toContainText("not found");
  expect(feed.actions).toEqual([]);
});
test("missing and wrong-scope entry never invent origins, and fresh evidence restores only its person", async ({
  page,
}) => {
  const feed = await recon(page, false);
  feed.objects([feed.person("person-1"), feed.person("person-2")]);
  for (const entry of [
    undefined,
    { bad: true },
    { ...feed.entry, map_epoch: 99 },
    { ...feed.entry, session_id: "other" },
  ]) {
    feed.setEntry(entry);
    await expect(page.getByTestId("person-found")).toHaveText("2 people found");
    await expect(page.getByTestId("recon-person").first()).toContainText(
      "mission entry was not recorded",
    );
    expect(feed.bodies).toHaveLength(0);
  }
  feed.setEntry(feed.entry);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(2);
  feed.objects([
    feed.person("person-1", 3, "not_found_on_rescan"),
    feed.person("person-2"),
  ]);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(1);
  feed.objects([feed.person("person-1"), feed.person("person-2")]);
  await expect.poll(() => feed.bodies.length).toBe(3);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(2);
  expect(feed.bodies[2].object_id).toBe("person-1");
  feed.reset();
  await expect(page.getByTestId("route-card")).toHaveCount(0);
});
test("manual approach selection and clearing never hide other automatic people", async ({
  page,
}) => {
  const feed = await recon(page);
  feed.objects([feed.person("person-1"), feed.person("person-2")]);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(2);
  await panel(page, "Spatial memory");
  await page.locator(".objects-panel .object-row").first().click();
  await page.getByRole("button", { name: "Suggest approach route" }).click();
  await expect(page.getByTestId("route-card")).toContainText(
    "Click the entrance or start point",
  );
  await expect(page.getByTestId("route-approach-label")).toHaveCount(2);
  await page
    .locator(".scene-canvas canvas")
    .click({ position: { x: 900, y: 800 } });
  await expect(page.getByTestId("route-approach-label")).toHaveCount(3);
  expect(feed.bodies.filter((body) => body.purpose === "recon")).toHaveLength(
    2,
  );
  const manual = feed.bodies.find((body) => body.purpose !== "recon")!;
  expect(manual.start).not.toEqual([-1, -1]);
  await page.getByRole("button", { name: "Clear approach route" }).click();
  await expect(page.getByTestId("route-approach-label")).toHaveCount(2);
  await expect(page.getByTestId("person-found")).toHaveText("2 people found");
  expect(feed.actions).toEqual([]);
});
test("all branches require fresh person evidence after reconnect and expire independently", async ({
  page,
}) => {
  const feed = await recon(page);
  feed.objects([feed.person("person-1"), feed.person("person-2")]);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(2);
  const openings = feed.openings();
  feed.disconnect();
  await expect.poll(feed.openings).toBeGreaterThan(openings);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  feed.occupancy();
  await page.waitForTimeout(200);
  expect(feed.bodies).toHaveLength(2);
  feed.objects([feed.person("person-1"), feed.person("person-2")]);
  await expect.poll(() => feed.bodies.length).toBe(4);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(2);
  await page.clock.setFixedTime(new Date(feed.now + 31000));
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  feed.objects([
    { ...feed.person("person-1"), last_seen: (feed.now + 31000) / 1000 },
    feed.person("person-2"),
  ]);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(1);
  expect(feed.bodies).toHaveLength(5);
  expect(feed.actions).toEqual([]);
});
for (const condition of ["map reset", "source change"] as const)
  test(`late automatic replies cannot restore any branch after ${condition}`, async ({
    page,
  }) => {
    const feed = await recon(page, true, true);
    feed.objects([
      feed.person("person-1"),
      feed.person("person-2"),
      feed.person("person-3"),
    ]);
    await expect.poll(() => feed.bodies.length).toBe(2);
    if (condition === "map reset") feed.reset();
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
    await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
    expect(feed.bodies).toHaveLength(2);
    expect(feed.actions).toEqual([]);
  });

test("retained display-only people never keep expired routes, and all route meshes use the confirmed floor", async ({
  page,
}) => {
  await page.addInitScript(() => {
    const w = window as any;
    w.__reconScenes = [];
    const hook = new EventTarget();
    hook.addEventListener("observe", (event) => {
      const scene = (event as CustomEvent).detail;
      if (scene.isScene) w.__reconScenes.push(scene);
    });
    w.__THREE_DEVTOOLS__ = hook;
  });
  const feed = await recon(page);
  feed.objects([feed.person("person-1"), feed.person("person-2")]);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(2);
  feed.send({
    version: 1,
    type: "occupancy",
    ...feed.scope,
    origin: [-2, -2],
    cell_m: 0.1,
    width: 50,
    height: 50,
    cells: Buffer.alloc(2500, 1).toString("base64"),
    floor_y: -0.35,
  });
  await expect
    .poll(async () =>
      page.evaluate(() => {
        const scene = (window as any).__reconScenes.find((s: any) =>
          s.getObjectByName("floor-grid"),
        );
        const pins: number[] = [],
          lineHeights: number[] = [];
        scene?.traverse((o: any) => {
          if (o.parent?.name !== "approach-route" || !o.isMesh) return;
          const starts = o.geometry.attributes.instanceStart;
          if (starts) {
            const ends = o.geometry.attributes.instanceEnd;
            for (let i = 0; i < starts.count; i++)
              lineHeights.push(
                starts.getY(i) + o.position.y,
                ends.getY(i) + o.position.y,
              );
          } else pins.push(o.position.y);
        });
        return {
          pins,
          linesOnFloor:
            lineHeights.length > 0 &&
            lineHeights.every((y) => Math.abs(y + 0.26) < 1e-6),
        };
      }),
    )
    .toEqual({ pins: [-0.26, -0.26, -0.26, -0.26], linesOnFloor: true });
  feed.send({
    version: 1,
    type: "detections",
    ...feed.scope,
    frame_id: 2,
    t_capture: 2,
    t_wall_ms: feed.now,
    image: { width: 80, height: 60 },
    source: "backend_detector",
    classes: ["person"],
    detections: [
      {
        class: "person",
        confidence: 0.9,
        box: [20, 10, 40, 50],
        position: [1, 0.2, 1],
        depth_m: 1,
        object_id: "person-1",
      },
    ],
  });
  await expect(page.getByTestId("live-detection-label")).toHaveCount(1);
  await page.clock.setFixedTime(new Date(feed.now + 31000));
  await expect(page.getByTestId("retained-person-label")).toHaveCount(1);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  feed.objects([
    { ...feed.person("person-1"), last_seen: (feed.now + 31000) / 1000 },
    feed.person("person-2"),
  ]);
  await expect(page.getByTestId("route-approach-label")).toHaveCount(1);
  await expect(page.getByTestId("retained-person-label")).toHaveCount(1);
});
