import { expect, test } from "@playwright/test";
import { capture } from "./captureFixture";
import { binaryPoints } from "./support/binaryPoints";

test("dense binary points share a map with compact RGB-D and covered points leave the draw list", async ({
  page,
}) => {
  let bodies = 0,
    conditional = 0;
  const headers = {
    ETag: '"compact-7"',
    "Access-Control-Expose-Headers": "ETag, X-Capture-Age-Ms",
  };
  await page.routeWebSocket("ws://localhost:9878/live", (ws) => {
    ws.send(
      JSON.stringify({
        version: 1,
        type: "objects",
        session_id: "surface-room",
        map_epoch: 1,
        objects: [],
      }),
    );
    ws.send(
      Buffer.from(
        binaryPoints(
          [8, 0, -2, 9, 0, -2, 8, 1, -2],
          [255, 0, 0, 0, 255, 0, 0, 0, 255],
          { session_id: "surface-room", map_epoch: 1, t_capture: 1 },
        ),
      ),
    );
  });
  await page.route("http://localhost:9878/**", (route) => {
    if (!route.request().url().endsWith("/capture/surface.bin"))
      return route.fulfill({ status: 404 });
    if (route.request().headers()["if-none-match"] === '"compact-7"') {
      conditional++;
      return route.fulfill({ status: 304, headers });
    }
    bodies++;
    return route.fulfill({
      body: capture(1, 2),
      contentType: "application/octet-stream",
      headers: { ...headers, "X-Capture-Age-Ms": "5" },
    });
  });
  await page.goto("/?live=ws%3A%2F%2Flocalhost%3A9878%2Flive");
  const status = page.getByTestId("viewport-status");
  await expect(status).toContainText("303 live points");
  await expect(status).toContainText("3 dots drawn");
  await expect.poll(() => conditional).toBeGreaterThan(0);
  expect(bodies).toBe(1);
  await expect(page.locator("canvas")).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(async () => {
        const url = "/node_modules/.vite/deps/@react-three_fiber.js";
        const { _roots } = await import(url);
        const scene = _roots
          .get(document.querySelector("canvas"))
          ?.store.getState().scene;
        return scene?.getObjectByName("live-point-cloud")?.geometry.drawRange
          .count;
      }),
    )
    .toBe(3);
});
