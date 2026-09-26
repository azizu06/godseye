import { test, expect } from "@playwright/test";
import { capture } from "./captureFixture";
import { closeWorkspace, panel, workspaceAction } from "./helpers";

for (const reset of [false, true])
  test(
    reset
      ? "map reset removes an early photo patch and rejects old capture identity during fusion"
      : "same-frame photo surfaces appear before persistent-map fusion completes",
    async ({ page }) => {
      // The real phone service is outside this test; all traffic stays in local mocks.
      await page.routeWebSocket(
        /ws:\/\/(localhost|127\.0\.0\.1):8765\//,
        (socket) => socket.close(),
      );
      await page.route(/http:\/\/(localhost|127\.0\.0\.1):8765\//, (route) =>
        route.abort(),
      );
      let resetMap = () => {};
      await page.addInitScript(() => {
        const NativeWorker = window.Worker;
        const state = window as typeof window & {
          qualityFusionStarted?: boolean;
          releaseQualityFusion?: () => void;
        };
        window.Worker = class extends NativeWorker {
          constructor(url: string | URL, options?: WorkerOptions) {
            super(url, options);
            if (!String(url).includes("surfaceMap.worker")) return;
            let released = false;
            this.addEventListener(
              "message",
              (event) => {
                // Decode and preview now also run off-thread. Hold the final fusion
                // response while allowing its earlier image/point preview through.
                if (!event.data.delta || released) return;
                event.stopImmediatePropagation();
                state.qualityFusionStarted = true;
                state.releaseQualityFusion = () => {
                  released = true;
                  this.dispatchEvent(
                    new MessageEvent("message", { data: event.data }),
                  );
                };
              },
              { capture: true },
            );
          }
        };
      });
      await page.routeWebSocket("ws://localhost:9876/live", (socket) => {
        const update = () =>
          socket.send(
            JSON.stringify({
              version: 1,
              type: "health",
              phone: "ok",
              car: "down",
              detector: "down",
              pose_age_ms: 0,
              mode: "manual",
              armed: false,
              stop_reason: null,
            }),
          );
        update();
        socket.send(
          JSON.stringify({
            version: 1,
            type: "objects",
            session_id: "surface-room",
            map_epoch: 1,
            objects: [],
          }),
        );
        resetMap = () =>
          socket.send(
            JSON.stringify({
              version: 1,
              type: "objects",
              session_id: "surface-room",
              map_epoch: 2,
              objects: [],
            }),
          );
        const timer = setInterval(update, 100);
        socket.onClose(() => clearInterval(timer));
      });
      await page.route("http://localhost:9876/**", (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path === "/capture/status")
          return route.fulfill({
            json: {
              tracking: "normal",
              frame: { capture_id: "photo-7", age_ms: 0 },
            },
          });
        if (path === "/capture/frame.bin")
          return route.fulfill({
            body: capture(),
            contentType: "application/octet-stream",
          });
        return route.fulfill({ json: { version: 1, events: [] } });
      });
      await page.goto("/");
      await workspaceAction(page, "Connection settings");
      await page
        .getByLabel("Telemetry WebSocket")
        .fill("ws://localhost:9876/live");
      await page.getByLabel("Backend API base").fill("http://localhost:9876");
      await page.getByLabel("Enable REST commands").uncheck();
      await page
        .getByRole("button", { name: "Connect source", exact: true })
        .click();
      await closeWorkspace(page);
      await expect
        .poll(() =>
          page.evaluate(() =>
            Boolean(
              (window as typeof window & { qualityFusionStarted?: boolean })
                .qualityFusionStarted,
            ),
          ),
        )
        .toBe(true);
      await panel(page, "Scene settings");
      await expect(page.getByTestId("surface-status")).toContainText(
        "color triangles",
      );
      // The worker is still held: this geometry must be the calibrated photo patch.
      await expect(page.locator(".scene-stat > span")).toHaveText(
        "POINTS RECEIVED",
      );
      const textureCount = await page.evaluate(async () => {
        const url = "/node_modules/.vite/deps/@react-three_fiber.js";
        const { _roots } = await import(url);
        const { scene } = _roots
          .get(document.querySelector("canvas"))
          .store.getState();
        let textured = 0;
        scene
          .getObjectByName("observed-color-surfaces")
          ?.traverse((object: { material?: { map?: unknown } }) => {
            if (object.material?.map) textured++;
          });
        return textured;
      });
      expect(textureCount).toBeGreaterThan(0);
      if (reset) {
        resetMap();
        await expect(page.getByTestId("surface-status")).not.toContainText(
          "color triangles",
        );
      }
      await page.evaluate(() =>
        (
          window as typeof window & { releaseQualityFusion?: () => void }
        ).releaseQualityFusion?.(),
      );
      if (reset) {
        // The mocked endpoint continues offering epoch1; it must not republish it.
        await page.waitForTimeout(800);
        await expect(page.getByTestId("surface-status")).not.toContainText(
          "color triangles",
        );
        await expect(page.locator(".scene-stat > span")).toHaveText(
          "POINTS RECEIVED",
        );
        return;
      }
      await expect(page.locator(".scene-stat > span")).toHaveText(
        "MAP VERTICES",
      );
      await expect(page.getByTestId("surface-status")).toContainText(
        "color triangles",
      );
    },
  );
