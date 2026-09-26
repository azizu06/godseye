import { expect, test, type Page } from "@playwright/test";

async function openViewport(page: Page) {
  await page.goto("/?live=off");
  const canvas = page.getByRole("application", {
    name: "Interactive 3D viewport",
  });
  await expect(canvas).toBeVisible();
  await canvas.focus();
  return canvas;
}

async function drag(page: Page, button: "right" | "left" | "middle") {
  await page.mouse.move(650, 420);
  await page.mouse.down({ button });
  await page.mouse.move(830, 510, { steps: 15 });
  await page.mouse.up({ button });
}

test("opens a full-window viewport without the old dashboard or backend connections", async ({
  page,
}) => {
  const sockets: string[] = [];
  const commands: string[] = [];
  const errors: string[] = [];
  page.on("websocket", (socket) => {
    if (!socket.url().includes(":5173")) sockets.push(socket.url());
  });
  page.on("request", (request) => {
    if (request.method() === "POST") commands.push(request.url());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  const canvas = await openViewport(page);
  await expect(page).toHaveTitle("God's Eye — 3D Viewport");
  expect(await canvas.boundingBox()).toMatchObject({
    x: 0,
    y: 0,
    width: 1440,
    height: 1100,
  });
  await expect(
    page.getByText("User Perspective", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button")).toHaveCount(0);
  await expect(page.getByRole("navigation")).toHaveCount(0);
  await expect(page.getByText("Spatial memory", { exact: true })).toHaveCount(
    0,
  );
  expect(sockets).toEqual([]);
  expect(commands).toEqual([]);
  expect(errors).toEqual([]);
});

for (const action of ["orbit", "pan", "left-pan", "zoom"] as const) {
  test(`${action} changes the rendered view and Home returns to the original camera`, async ({
    page,
  }) => {
    const canvas = await openViewport(page);
    const initial = await canvas.screenshot();
    if (action === "pan") await page.keyboard.down("Shift");
    if (action === "zoom") {
      await page.mouse.move(700, 500);
      await page.mouse.wheel(0, -500);
    } else await drag(page, action === "left-pan" ? "left" : "right");
    if (action === "pan") await page.keyboard.up("Shift");
    await expect
      .poll(async () => (await canvas.screenshot()).equals(initial))
      .toBe(false);
    await canvas.press("Home");
    await expect
      .poll(async () => (await canvas.screenshot()).equals(initial))
      .toBe(true);
  });
}

test("keyboard navigation works", async ({ page }) => {
  const canvas = await openViewport(page);
  await canvas.click({ position: { x: 500, y: 400 } });
  const initial = await canvas.screenshot();
  await canvas.press("Shift+ArrowLeft");
  await expect
    .poll(async () => (await canvas.screenshot()).equals(initial))
    .toBe(false);
  await canvas.press("Home");
  const reset = await canvas.screenshot();
  await canvas.press("ArrowUp");
  await expect
    .poll(async () => (await canvas.screenshot()).equals(reset))
    .toBe(false);
});

test("viewport follows window resizing without page scroll", async ({
  page,
}) => {
  const canvas = await openViewport(page);
  await page.setViewportSize({ width: 420, height: 780 });
  await expect
    .poll(() => canvas.boundingBox())
    .toMatchObject({ x: 0, y: 0, width: 420, height: 780 });
  const dimensions = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    scrollHeight: document.documentElement.scrollHeight,
    width: innerWidth,
    height: innerHeight,
  }));
  expect(dimensions.scrollWidth).toBe(dimensions.width);
  expect(dimensions.scrollHeight).toBe(dimensions.height);
});
