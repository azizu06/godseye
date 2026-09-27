import { expect, test } from "@playwright/test";
import { closeWorkspace, panel } from "./helpers";

// Stored objects shaped like the assessed handheld scan: one well-evidenced
// object, one-off weak detections, a once-seen person and an object last seen
// ten minutes ago. The harness is the backend; the app only renders.
const scope = { session_id: "room", map_epoch: 1 };
const nowS = () => Date.now() / 1000;
const object = (over: Record<string, unknown>) => ({
  position: [0, 0.3, 2],
  confidence: 0.8,
  first_seen: nowS() - 5,
  last_seen: nowS(),
  observations: 6,
  state: "present",
  ...over,
});

test("3D labels hide low-evidence objects, keep possible people, word stale objects by age and keep raw access", async ({
  page,
}) => {
  await page.routeWebSocket("ws://localhost:9879/live", (ws) => {
    ws.send(
      JSON.stringify({
        version: 1,
        type: "objects",
        ...scope,
        objects: [
          object({ id: "chair-1", class: "chair", position: [-1.5, 0.3, 2] }),
          object({
            id: "frisbee-1",
            class: "frisbee",
            position: [1.5, 0.3, 2],
            confidence: 0.39,
            observations: 2,
          }),
          object({
            id: "person-weak",
            class: "person",
            position: [0, 0.3, 3],
            confidence: 0.27,
            observations: 1,
          }),
          object({
            id: "person-old",
            class: "person",
            position: [0, 0.3, 0.5],
            last_seen: nowS() - 600,
          }),
        ],
      }),
    );
  });
  await page.route("http://localhost:9879/**", (route) =>
    route.fulfill({ status: 404 }),
  );
  await page.goto("/?live=ws%3A%2F%2Flocalhost%3A9879%2Flive");

  const labels = page.locator(".scene-label");
  await expect(labels).toHaveCount(3);
  await expect(labels.filter({ hasText: "Frisbee" })).toHaveCount(0);
  await expect(
    page.locator('.scene-label[data-evidence="possible_person"]'),
  ).toHaveText("Person?27%");
  const old = page.locator(".scene-label.stale");
  await expect(old).toHaveCount(1);
  await expect(old).toContainText("10m ago");

  // Raw access: every stored object stays listed with its evidence.
  await panel(page, "Spatial memory");
  const rows = page.locator(".object-row");
  await expect(rows).toHaveCount(4);
  await expect(rows.filter({ hasText: "Frisbee" })).toContainText(
    "39% · 2 frames · low evidence, hidden in 3D",
  );
  await expect(rows.filter({ hasText: "Person" }).first()).toBeVisible();
  await expect(page.getByTestId("possible-person")).toHaveCount(1);
  await expect(rows.filter({ hasText: "Last seen 10m ago" })).toHaveCount(1);

  // Selecting a hidden object draws it; the inspector never says Present for a stale one.
  await rows.filter({ hasText: "Last seen 10m ago" }).click();
  await panel(page, "Object intelligence");
  await expect(page.locator(".state-pill")).toHaveText("Last seen 10m ago");
  await panel(page, "Spatial memory");
  await rows.filter({ hasText: "Frisbee" }).click();
  await expect(labels.filter({ hasText: "Frisbee" })).toHaveCount(1);
  await closeWorkspace(page);

  // Scene layers can show every stored object in 3D.
  await panel(page, "Scene settings");
  await expect(page.getByTestId("weak-hidden")).toHaveText(
    "· 1 low-evidence hidden in 3D",
  );
  await page.getByRole("button", { name: "Scene layers" }).click();
  await page.getByLabel("Low-evidence objects (1)").check();
  await expect(labels).toHaveCount(4);
  await expect(page.getByTestId("weak-hidden")).toHaveCount(0);
});

test("overlapping 3D labels yield to people and reappear when separated", async ({
  page,
}) => {
  let send: (value: unknown) => void = () => {};
  const at = (position: number[]) => ({
    version: 1,
    type: "objects",
    ...scope,
    objects: [
      object({ id: "person-1", class: "person", position: [0, 0.3, 2] }),
      object({ id: "chair-1", class: "chair", position }),
    ],
  });
  await page.routeWebSocket("ws://localhost:9879/live", (ws) => {
    send = (value) => ws.send(JSON.stringify(value));
    send(at([0.02, 0.3, 2]));
  });
  await page.route("http://localhost:9879/**", (route) =>
    route.fulfill({ status: 404 }),
  );
  await page.goto("/?live=ws%3A%2F%2Flocalhost%3A9879%2Flive");

  const chair = page
    .locator(".projected-label")
    .filter({ has: page.locator(".scene-label", { hasText: "Chair" }) });
  const person = page
    .locator(".projected-label")
    .filter({ has: page.locator(".scene-label", { hasText: "Person" }) });
  await expect(chair).toHaveAttribute("data-overlap-hidden", "true");
  await expect(person).toHaveAttribute("data-overlap-hidden", "false");
  await expect(person).toBeVisible();
  await expect(chair).toBeHidden();

  send(at([-3, 0.3, 2]));
  await expect(chair).toHaveAttribute("data-overlap-hidden", "false");
  await expect(chair).toBeVisible();
});
