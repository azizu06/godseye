import { expect, test } from "@playwright/test";
import { panel } from "./helpers";

const scope = { session_id: "room", map_epoch: 1 };
const occupancy = (fill: number) => ({
  version: 1,
  type: "occupancy",
  ...scope,
  origin: [-2, -2],
  cell_m: 0.05,
  width: 80,
  height: 80,
  cells: Buffer.alloc(6400, fill).toString("base64"),
});
const assumptions = {
  walker_radius_m: 0.25,
  margin_m: 0.05,
  person_keep_out_m: 0.3,
  approach_reach_m: 1.5,
  unknown: "blocked",
  doors: "not_inferred",
  verified: false,
};

test("an operator-started approach route to a person is drawn, rechecked on new evidence and retired on reset", async ({
  page,
}) => {
  let send: (value: unknown) => void = () => {};
  const routeBodies: Record<string, unknown>[] = [];
  const otherPosts: string[] = [];
  await page.routeWebSocket("ws://localhost:9880/live", (ws) => {
    send = (value) => ws.send(JSON.stringify(value));
    const health = () =>
      send({
        version: 1,
        type: "health",
        ...scope,
        phone: "ok",
        car: "down",
        detector: "ok",
        pose_age_ms: 10,
        mode: "manual",
        armed: false,
        stop_reason: null,
      });
    health();
    const timer = setInterval(health, 100);
    ws.onClose(() => clearInterval(timer));
    send({
      version: 1,
      type: "objects",
      ...scope,
      objects: [
        {
          id: "person-1",
          class: "person",
          position: [1, 0.2, 1],
          confidence: 0.9,
          first_seen: Date.now() / 1000,
          last_seen: Date.now() / 1000,
          observations: 3,
          state: "present",
        },
      ],
    });
    send(occupancy(1));
  });
  await page.route("http://localhost:9880/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path !== "/route") otherPosts.push(path);
    if (path !== "/route") return route.fulfill({ status: 404 });
    const body = request.postDataJSON();
    routeBodies.push(body);
    // First plan succeeds; after new occupancy evidence the route is gone.
    return route.fulfill({
      json:
        routeBodies.length === 1
          ? {
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
              assumptions,
            }
          : {
              version: 1,
              ...scope,
              object_id: body.object_id,
              start: body.start,
              person: [1, 1],
              occupancy_revision: 2,
              status: "unavailable",
              reason: "no_observed_free_route",
              assumptions,
            },
    });
  });
  await page.goto(
    "/?live=ws%3A%2F%2Flocalhost%3A9880%2Flive&api=http%3A%2F%2Flocalhost%3A9880",
  );
  await panel(page, "Spatial memory");
  await page.locator(".objects-panel .object-row").first().click();
  await page.getByRole("button", { name: "Suggest approach route" }).click();
  const card = page.getByTestId("route-card");
  await expect(card).toContainText("Click the entrance or start point");
  await expect(card).toContainText("visualization only");

  // Any floor point away from the object label: the start is the operator's choice.
  await page
    .locator(".scene-canvas canvas")
    .click({ position: { x: 900, y: 800 } });
  await expect(page.getByTestId("route-status")).toContainText(
    "Route 1.4 m to an observed-free approach point beside the person · unverified · not a rover path",
  );
  await expect(card).toContainText("assumes a 0.5 m wide walker");
  await expect(card).toContainText(
    "unknown space blocked · doors not inferred",
  );
  await expect(page.getByTestId("route-start-label")).toBeVisible();
  await expect(page.getByTestId("route-approach-label")).toBeVisible();
  expect(routeBodies).toHaveLength(1);
  expect(routeBodies[0]).toMatchObject({ ...scope, object_id: "person-1" });

  // Changed blocking evidence rechecks the route; an unsupported route is not drawn.
  send(occupancy(2));
  await expect(page.getByTestId("route-status")).toContainText(
    "Route unavailable · no observed-free connection",
  );
  await expect(page.getByTestId("route-approach-label")).toHaveCount(0);
  expect(routeBodies).toHaveLength(2);

  // A new map retires the route entirely.
  send({
    version: 1,
    type: "objects",
    session_id: "room",
    map_epoch: 2,
    objects: [],
  });
  await expect(card).toHaveCount(0);
  await expect(page.getByTestId("route-start-label")).toHaveCount(0);
  expect(otherPosts).toEqual([]); // no goal, arm or motion request
});
