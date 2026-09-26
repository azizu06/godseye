import { test, expect } from "@playwright/test";
import { simulator, closeWorkspace, panel, workspace } from "./helpers";

test("default viewport is minimal and workspace tools remain keyboard accessible", async ({
  page,
}) => {
  await simulator(page);
  await expect(page.locator("canvas")).toBeVisible();
  const mainButtons = page.locator(
    ".scene-toolbar button:visible, .flight-controls button:visible",
  );
  await expect(mainButtons).toHaveText([
    "3D",
    "2D",
    "Standard",
    "Explore",
    "Arm",
  ]);
  await expect(
    page.locator(
      ".workspace-drawer, .scene-stat, .surface-status, .workspace-dock, .status-footer",
    ),
  ).toHaveCount(0);
  expect(
    (await page.locator(".scene-toolbar").boundingBox())!.y,
  ).toBeGreaterThanOrEqual(12);
  expect(
    (await page.locator(".flight-controls").boundingBox())!.y,
  ).toBeGreaterThanOrEqual(12);
  await page.screenshot({ path: "/tmp/godseye-minimal-desktop.png" });
  for (const key of ["Escape", "ContextMenu", "Shift+F10"]) {
    await page.keyboard.press(key);
    await expect(
      page.getByRole("dialog", { name: "Workspace", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("navigation", { name: "Workspace panels" }),
    ).toBeVisible();
    await closeWorkspace(page);
  }
  await panel(page, "Scene settings");
  await expect(page.getByTestId("surface-status")).toContainText(
    "Coarse preview",
  );
  await expect(
    page.getByRole("button", { name: "Scene layers" }),
  ).toBeVisible();
  await page.screenshot({ path: "/tmp/godseye-minimal-scene-settings.png" });
  await closeWorkspace(page);
  await page.getByRole("button", { name: "3D", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(mainButtons).toHaveText([
    "3D",
    "2D",
    "Standard",
    "Explore",
    "Arm",
  ]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(
    (await page.locator(".scene-toolbar").boundingBox())!.y,
  ).toBeGreaterThanOrEqual(12);
  expect(
    (await page.locator(".flight-controls").boundingBox())!.y,
  ).toBeGreaterThanOrEqual(12);
  await page.screenshot({ path: "/tmp/godseye-minimal-mobile.png" });
  await workspace(page);
  await expect(
    page.getByRole("button", { name: "Export snapshot", exact: true }),
  ).toBeVisible();
});

test("stationary right click opens workspace, right drag stays a view gesture, and drawer arrows cannot drive", async ({
  page,
}) => {
  await simulator(page);
  await page
    .locator("canvas")
    .click({ button: "right", position: { x: 600, y: 350 } });
  await expect(
    page.getByRole("dialog", { name: "Workspace", exact: true }),
  ).toBeVisible();
  await closeWorkspace(page);
  await page.mouse.move(650, 350);
  await page.mouse.down({ button: "right" });
  await page.mouse.move(750, 420, { steps: 8 });
  await page.mouse.up({ button: "right" });
  await expect(page.locator(".workspace-drawer")).toHaveCount(0);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await page
    .getByRole("button", { name: "Arm simulator", exact: true })
    .click();
  const rover = page.locator("svg.map2d g[transform*=rotate]");
  const before = await rover.getAttribute("transform");
  await page.keyboard.down("ArrowUp");
  await expect(rover).not.toHaveAttribute("transform", before!);
  await page.mouse.click(600, 350, { button: "right" });
  await page.keyboard.up("ArrowUp");
  await expect(
    page.getByRole("dialog", { name: "Workspace", exact: true }),
  ).toBeVisible();
  await page.waitForTimeout(150);
  const stopped = await rover.getAttribute("transform");
  await page.keyboard.down("ArrowDown");
  await page.waitForTimeout(400);
  await page.keyboard.up("ArrowDown");
  await expect(rover).toHaveAttribute("transform", stopped!);
});

test("touch long press opens workspace and a second touch cancels the gesture", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await simulator(page);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await page
    .getByRole("button", { name: "Arm simulator", exact: true })
    .click();
  const rover = page.locator("svg.map2d g[transform*=rotate]");
  const before = await rover.getAttribute("transform");
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: true });
  const first = { x: 180, y: 350, id: 1 };
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [first],
  });
  await expect(
    page.getByRole("dialog", { name: "Workspace", exact: true }),
  ).toBeVisible();
  // Hold beyond the old fixed suppression window; releasing must not set a goal.
  await page.waitForTimeout(1500);
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchEnd",
    touchPoints: [],
  });
  await page.waitForTimeout(350);
  await expect(rover).toHaveAttribute("transform", before!);
  await closeWorkspace(page);
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [first],
  });
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [first, { x: 220, y: 350, id: 2 }],
  });
  await page.waitForTimeout(700);
  await expect(page.locator(".workspace-drawer")).toHaveCount(0);
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchEnd",
    touchPoints: [],
  });
});

test("left drag translates the view and right drag rotates around its target", async ({
  page,
}) => {
  await simulator(page);
  // Read the actual renderer camera to distinguish panning from rotation.
  // This does not add a production debug endpoint or change camera behavior.
  const camera = () =>
    page.evaluate(async () => {
      const moduleUrl = "/node_modules/.vite/deps/@react-three_fiber.js";
      const { _roots } = await import(moduleUrl);
      const { camera, controls } = _roots
        .get(document.querySelector("canvas"))
        .store.getState();
      return {
        position: camera.position.toArray() as number[],
        quaternion: camera.quaternion.toArray() as number[],
        target: controls.target.toArray() as number[],
      };
    });
  const distance = (a: number[], b: number[]) =>
    Math.hypot(...a.map((v, i) => v - b[i]));
  const settledCamera = async () => {
    let previous = await camera(),
      stable = 0;
    await expect
      .poll(
        async () => {
          const current = await camera();
          const delta =
            distance(current.position, previous.position) +
            distance(current.target, previous.target) +
            distance(current.quaternion, previous.quaternion);
          stable = delta < 0.0001 ? stable + 1 : 0;
          previous = current;
          return stable;
        },
        { intervals: [150], timeout: 15000 },
      )
      .toBeGreaterThanOrEqual(3);
    return previous;
  };
  const before = await settledCamera();
  await page.mouse.move(600, 300);
  await page.mouse.down({ button: "left" });
  await page.mouse.move(760, 370, { steps: 12 });
  await page.mouse.up({ button: "left" });
  const panned = await settledCamera();
  expect(distance(panned.position, before.position)).toBeGreaterThan(0.2);
  expect(distance(panned.target, before.target)).toBeGreaterThan(0.2);
  expect(distance(panned.quaternion, before.quaternion)).toBeLessThan(1e-5);
  const cameraDelta = panned.position.map((v, i) => v - before.position[i]);
  const targetDelta = panned.target.map((v, i) => v - before.target[i]);
  expect(distance(cameraDelta, targetDelta)).toBeLessThan(1e-5);
  await page.mouse.move(600, 300);
  await page.mouse.down({ button: "right" });
  await page.mouse.move(760, 370, { steps: 12 });
  await page.mouse.up({ button: "right" });
  const rotated = await settledCamera();
  expect(distance(rotated.quaternion, panned.quaternion)).toBeGreaterThan(0.05);
  expect(distance(rotated.target, panned.target)).toBeLessThan(0.01);
  expect(distance(rotated.position, rotated.target)).toBeCloseTo(
    distance(panned.position, panned.target),
    3,
  );
  await expect(page.locator(".workspace-drawer")).toHaveCount(0);
});
