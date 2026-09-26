import { expect, test, type Page, type WebSocketRoute } from "@playwright/test";

const source = "ws://127.0.0.1:8797/live";
function wall(id = 1, epoch = 1, color = [1, 0.1, 0.05]) {
  const positions: number[] = [],
    colors: number[] = [];
  for (let y = 0; y < 30; y++)
    for (let x = 0; x < 30; x++) {
      positions.push((x - 15) * 0.04, 1 + y * 0.04, -2);
      colors.push(...color);
    }
  return {
    version: 1,
    type: "points",
    session_id: "synthetic-test",
    map_epoch: epoch,
    chunk_id: id,
    t_capture: id,
    positions,
    colors,
  };
}
async function openFeed(page: Page) {
  const connections: WebSocketRoute[] = [];
  await page.routeWebSocket(source, (socket) => {
    connections.push(socket);
  });
  await page.goto(`/?live=${encodeURIComponent(source)}`);
  const status = page.getByRole("status", { name: "Point cloud status" });
  await expect(status).toContainText("Waiting for RGB + depth");
  const canvas = page.getByRole("application", {
    name: "Interactive 3D viewport",
  });
  await expect(canvas).toBeVisible();
  const send = (value: unknown) =>
    connections.at(-1)!.send(JSON.stringify(value));
  // Crop out status/hints so comparisons measure rendered geometry only.
  const rendered = () =>
    page.screenshot({ clip: { x: 250, y: 120, width: 900, height: 780 } });
  return { canvas, status, send, rendered, connections };
}

test("live RGB-depth points render, update color, and never steer the viewing camera", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const { canvas, status, send, rendered } = await openFeed(page);
  const empty = await rendered();
  send(wall());
  await expect(status).toContainText("900 points");
  await expect.poll(async () => (await rendered()).equals(empty)).toBe(false);
  await canvas.press("f");
  const red = await rendered();
  send(wall(2, 1, [0.05, 0.1, 1]));
  await expect.poll(async () => (await rendered()).equals(red)).toBe(false);
  const blue = await rendered();
  // A radically different phone pose must not move the inspection camera.
  send({
    version: 1,
    type: "pose",
    tracking: "normal",
    position: [100, 100, 100],
  });
  send(wall(3, 1, [0.05, 0.1, 1]));
  await expect(status).toContainText("Live RGB + depth");
  expect((await rendered()).equals(blue)).toBe(true);
  await canvas.press("ArrowLeft");
  await expect.poll(async () => (await rendered()).equals(blue)).toBe(false);
  await canvas.press("Home");
  const home = await rendered();
  await canvas.press("f");
  await expect.poll(async () => (await rendered()).equals(home)).toBe(false);
  expect(errors).toEqual([]);
});

test("map resets clear rendered points and invalid chunks cannot corrupt the next map", async ({
  page,
}) => {
  const { status, send, rendered } = await openFeed(page);
  const empty = await rendered();
  send(wall());
  await expect(status).toContainText("900 points");
  await expect.poll(async () => (await rendered()).equals(empty)).toBe(false);
  send({
    version: 1,
    type: "objects",
    session_id: "synthetic-test",
    map_epoch: 2,
  });
  await expect(status).toContainText("0 points");
  await expect.poll(async () => (await rendered()).equals(empty)).toBe(true);
  send(wall(99, 1)); // Delayed packet from a retired map.
  send({ ...wall(1, 2), colors: [1] });
  await expect(status).toContainText("1 invalid packets skipped");
  await expect(status).toContainText("0 points");
  send(wall(1, 2));
  await expect(status).toContainText("900 points");
});

test("disconnected scans stay visible, stop claiming live, and clear on reconnection", async ({
  page,
}) => {
  const { status, send, connections } = await openFeed(page);
  send(wall());
  await expect(status).toHaveAttribute("data-live", "true");
  send({ version: 1, type: "health", phone: "down" });
  await expect(status).toContainText("Phone offline");
  await expect(status).toHaveAttribute("data-live", "false");
  await expect(status).toContainText("900 points");
  send({ version: 1, type: "health", phone: "ok" });
  send({ version: 1, type: "pose", tracking: "limited" });
  await expect(status).toContainText("Tracking limited");
  await expect(status).toHaveAttribute("data-live", "false");
  const previous = connections.length;
  await connections.at(-1)!.close({ code: 1012, reason: "Fixture reconnect" });
  await expect(status).toContainText("Feed disconnected");
  await expect(status).toContainText("900 points");
  await expect.poll(() => connections.length).toBeGreaterThan(previous);
  await expect(status).toContainText("0 points");
  send(wall());
  await expect(status).toContainText("900 points");
});
