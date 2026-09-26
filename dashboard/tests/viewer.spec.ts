import { test, expect } from "@playwright/test";
import { panel, closeWorkspace } from "./helpers";

test("point-only feed is visible with controls off, survives reconnect, and frames the scan", async ({
  page,
}) => {
  let close = () => {},
    send = (_epoch: number, _chunk: number) => {};
  let connections = 0;
  let pointOffset = 0;
  let seed = true;
  const posts: string[] = [];
  await page.route("http://localhost:9876/**", (route) => {
    if (route.request().method() === "POST") posts.push(route.request().url());
    return route.fulfill({ json: { frame: null } });
  });
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    connections++;
    close = () => ws.close();
    const health = () =>
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone: "ok",
          car: "ok",
          detector: "ok",
          pose_age_ms: 0,
          mode: "manual",
          armed: false,
          stop_reason: null,
        }),
      );
    send = (epoch, chunk) => {
      ws.send(
        JSON.stringify({
          version: 1,
          type: "pose",
          session_id: "viewer",
          map_epoch: epoch,
          position: [20, 1, 0],
          yaw_rad: 0,
          tracking: "normal",
        }),
      );
      ws.send(
        JSON.stringify({
          version: 1,
          type: "points",
          session_id: "viewer",
          map_epoch: epoch,
          chunk_id: chunk,
          positions: [19, 0, -2, 21, 2, -2, 20, 1, -2].map((v, i) =>
            i % 3 === 0 ? v + pointOffset : v,
          ),
          colors: [1, 0, 0, 0, 1, 0, 0, 0, 1],
        }),
      );
    };
    health();
    if (seed) send(1, 1);
    const timer = setInterval(health, 100);
    ws.onClose(() => clearInterval(timer));
  });
  await page.goto("/?live=ws%3A%2F%2Flocalhost%3A9876%2Flive");
  const status = page.getByTestId("viewport-status");
  await expect(status).toContainText("External feed");
  await expect(status).toContainText("localhost:9876");
  await expect(status).toContainText("3 live points");
  await expect(status).toContainText("No RGB-D capture received");
  await expect(page.getByRole("button", { name: "Arm rover" })).toBeDisabled();
  await page.keyboard.press("ArrowUp");
  await page.locator("canvas").click({ position: { x: 650, y: 400 } });
  await page.getByRole("button", { name: "Frame scan", exact: true }).click();
  const rendered = async () =>
    page.evaluate(async () => {
      const module = "/node_modules/.vite/deps/@react-three_fiber.js";
      const { _roots } = await import(module);
      const state = _roots
        .get(document.querySelector("canvas"))
        .store.getState();
      const cloud = state.scene.getObjectByName("live-point-cloud");
      return {
        count: cloud?.geometry.drawRange.count,
        targetX: state.controls.target.x,
        frameloop: state.frameloop,
      };
    });
  await expect
    .poll(rendered)
    .toEqual({ count: 3, targetX: 20, frameloop: "demand" });
  seed = false;
  const beforeReconnect = connections;
  close();
  await expect.poll(() => connections).toBe(beforeReconnect + 1);
  await expect(status).toContainText("3 live points");
  // Restarted IDs must admit new observations while retaining the old scan.
  pointOffset = 10;
  send(1, 1);
  await expect(status).toContainText("6 live points");
  await expect.poll(async () => (await rendered()).count).toBe(6);
  await panel(page, "Scene settings");
  await page.getByRole("button", { name: "Scene layers", exact: true }).click();
  await page.getByLabel("Point cloud", { exact: true }).uncheck();
  await expect(status).toContainText("points hidden");
  await closeWorkspace(page);
  send(2, 1);
  await expect(status).toContainText("3 live points");
  expect(posts).toEqual([]);
});

import { capture } from "./captureFixture";
for (const version of [1, 2])
  test(`v${version} delayed calibrated RGB-D renders despite stale pose, then explains expiry and retains surfaces`, async ({
    page,
  }) => {
    let age = 10000;
    let requests = 0;
    const posts: string[] = [];
    await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
      const send = () => {
        ws.send(
          JSON.stringify({
            version: 1,
            type: "health",
            phone: "stale",
            car: "ok",
            detector: "ok",
            pose_age_ms: 5000,
            mode: "manual",
            armed: false,
            stop_reason: "pose_stale",
          }),
        );
        ws.send(
          JSON.stringify({
            version: 1,
            type: "pose",
            session_id: "surface-room",
            map_epoch: 1,
            position: [0, 1, 0],
            yaw_rad: 0,
            tracking: "limited",
          }),
        );
      };
      send();
      const timer = setInterval(send, 100);
      ws.onClose(() => clearInterval(timer));
    });
    await page.route("http://localhost:9876/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (route.request().method() === "POST") posts.push(path);
      if (path === "/capture/status")
        return route.fulfill({
          json: {
            tracking: "limited",
            frame:
              version === 1
                ? {
                    capture_id: "delayed",
                    age_ms: age,
                    metadata: { tracking: "normal" },
                  }
                : null,
            rich:
              version === 2
                ? {
                    packets: {
                      frame: {
                        token: "delayed-rich",
                        age_ms: age,
                        metadata: { tracking: "normal" },
                        sections: ["rgb", "raw_depth", "raw_confidence"].map(
                          (name) => ({ name }),
                        ),
                      },
                    },
                  }
                : undefined,
          },
        });
      if (path.endsWith("frame.bin")) {
        requests++;
        return route.fulfill({
          body: capture(1, version),
          contentType: "application/octet-stream",
        });
      }
      return route.fulfill({ json: {} });
    });
    await page.goto("/?live=ws%3A%2F%2Flocalhost%3A9876%2Flive");
    const status = page.getByTestId("viewport-status");
    await expect(status).toContainText("Rendering delayed RGB + depth");
    await expect(status).not.toContainText("0 surface triangles");
    await expect(
      page.getByRole("button", { name: "Arm rover" }),
    ).toBeDisabled();
    age = 16000;
    await expect(status).toContainText("RGB-D capture too old");
    await expect(status).not.toContainText("0 surface triangles");
    const prior = requests;
    await page.waitForTimeout(700);
    expect(requests).toBe(prior);
    expect(posts).toEqual([]);
  });
