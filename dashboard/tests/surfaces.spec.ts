import { test, expect } from "@playwright/test";
import { capture } from "./captureFixture";
test("default solid surfaces discover progressively and point cloud remains optional", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByTestId("surface-status")).toContainText(
    /color triangles/,
  );
  await page.getByRole("button", { name: "Scene layers", exact: true }).click();
  await expect(
    page.getByLabel("Color surfaces", { exact: true }),
  ).toBeChecked();
  await expect(
    page.getByLabel("Point cloud", { exact: true }),
  ).not.toBeChecked();
  await page.getByLabel("Point cloud", { exact: true }).check();
  await page.getByLabel("Color surfaces", { exact: true }).uncheck();
  await expect(page.getByTestId("surface-status")).toHaveCount(0);
  await page.getByLabel("Color surfaces", { exact: true }).check();
  await page.getByLabel("Point cloud", { exact: true }).uncheck();
  await page.getByRole("button", { name: "Scene layers", exact: true }).click();
  await page.waitForTimeout(5000);
  await page.screenshot({ path: "/tmp/godseye-color-surfaces.png" });
});
for (const version of [1, 2])
  test(`existing v${version} binary capture renders without drive permission and rejects late frames after a map reset`, async ({
    page,
  }) => {
    let epoch = 1;
    let release = () => {};
    let pending = false;
    const gate = new Promise<void>((r) => (release = r));
    await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
      const send = () => {
        ws.send(
          JSON.stringify({
            version: 1,
            type: "health",
            phone: "ok",
            car: "down",
            detector: "ok",
            pose_age_ms: 0,
            mode: "manual",
            armed: false,
            stop_reason: null,
            session_id: "surface-room",
            map_epoch: epoch,
          }),
        );
        ws.send(
          JSON.stringify({
            version: 1,
            type: "pose",
            position: [0, 1, 0],
            yaw_rad: Math.PI,
            tracking: "normal",
            session_id: "surface-room",
            map_epoch: epoch,
          }),
        );
      };
      send();
      const id = setInterval(send, 100);
      ws.onClose(() => clearInterval(id));
    });
    await page.route("http://localhost:9876/**", async (route) => {
      if (route.request().url().endsWith("/capture/status"))
        return route.fulfill({
          json: {
            tracking: "normal",
            rich:
              version === 2
                ? {
                    packets: {
                      frame: {
                        token: `rich-${epoch}`,
                        age_ms: 10,
                        sections: ["rgb", "raw_depth", "raw_confidence"].map(
                          (name) => ({ name }),
                        ),
                      },
                    },
                  }
                : undefined,
            frame: { capture_id: epoch === 1 ? "one" : "two", age_ms: 10 },
          },
        });
      if (
        route
          .request()
          .url()
          .endsWith(
            version === 2 ? "/capture/rich/frame.bin" : "/capture/frame.bin",
          )
      ) {
        if (epoch === 2) {
          pending = true;
          await gate;
        }
        return route.fulfill({
          body: capture(1, version),
          contentType: "application/octet-stream",
        });
      }
      return route.fulfill({ json: { version: 1, ok: true } });
    });
    await page.goto("/");
    await page
      .getByRole("button", { name: "Connection settings", exact: true })
      .click();
    await page.getByRole("button", { name: /External feed Connect/ }).click();
    await page
      .getByLabel("Telemetry WebSocket")
      .fill("ws://localhost:9876/live");
    await page.getByLabel("Backend API base").fill("http://localhost:9876");
    await page.getByLabel("Enable REST commands").uncheck();
    await expect(page.getByLabel("Backend API base")).toBeVisible();
    await page
      .getByRole("button", { name: "Connect source", exact: true })
      .click();
    await expect(page.getByTestId("surface-status")).toContainText(
      "color triangles",
    );
    await page.waitForTimeout(800);
    await page.screenshot({ path: "/tmp/godseye-live-color-surface.png" });
    epoch = 2;
    await expect.poll(() => pending).toBe(true);
    await expect(page.getByTestId("surface-status")).not.toContainText(
      "color triangles",
    );
    release();
    await page.waitForTimeout(500);
    await expect(page.getByTestId("surface-status")).not.toContainText(
      "color triangles",
    );
  });
