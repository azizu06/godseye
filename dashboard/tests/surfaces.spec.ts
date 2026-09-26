import { test, expect } from "@playwright/test";
const JPEG = Buffer.from(
  "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQEBAQIBAQECAgICAgQDAgICAgUEBAMEBgUGBgYFBgYGBwkIBgcJBwYGCAsICQoKCgoKBggLDAsKDAkKCgr/2wBDAQICAgICAgUDAwUKBwYHCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgr/wAARCAA8AFADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDj6KKK/nc/tQKKKKACvL69Qry+v6G8Bv8AmY/9wf8A3Kfyv9Jj/mVf9x//AHCFFFFf0MfyuFFFFAHqFFemf8MeftGf9E7/APKvZ/8Ax6j/AIY8/aM/6J3/AOVez/8Aj1f51fXcH/z8j96/zP8AUH+1cr/5/wAP/Ao/5nmdFemf8MeftGf9E7/8q9n/APHqP+GPP2jP+id/+Vez/wDj1H13B/8APyP3r/MP7Vyv/n/D/wACj/meZ15fX05/wx5+0Z/0Tv8A8q9n/wDHq8N/4Un8Tv8AoWf/ACdh/wDi6/oDwLzXK8P/AGh7WvCN/ZWvKK/5+92fzB9JHE4bF/2X7Canb29+Vp2v7HexytFdV/wpP4nf9Cz/AOTsP/xdH/Ck/id/0LP/AJOw/wDxdf0B/b2R/wDQVT/8Dj/mfy/yT7HK0V1X/Ck/id/0LP8A5Ow//F0f8KT+J3/Qs/8Ak7D/APF0f29kf/QVT/8AA4/5hyT7H6rUUUV/mAf2sFFFFABXxPX2xXxPX6p4Z/8AMX/3D/8Abz8b8W/+YL/uJ/7jCiiiv1Q/GwooooA+2KKKK/lc/sgKKKKACvievtiviev1Twz/AOYv/uH/AO3n434t/wDMF/3E/wDcYUUUV+qH42FFFFAH/9k=",
  "base64",
);
function capture(epoch = 1, version = 1) {
  const header = {
    version: 1,
    type: "frame",
    session_id: "surface-room",
    map_epoch: epoch,
    frame_id: 7,
    t_capture: 3.5,
    t_wall_ms: Date.now(),
    tracking: "normal",
    transform: [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 1],
    image: {
      width: 80,
      height: 60,
      jpeg_len: JPEG.length,
      intrinsics: [40, 0, 0, 0, 40, 0, 40, 30, 1],
      orientation: "landscape_right",
    },
    depth: { width: 20, height: 15, format: "float32_m", len: 1200 },
    confidence: { width: 20, height: 15, format: "uint8_0_2", len: 300 },
  };
  const rich = {
    ...header,
    version: 2,
    type: "capture",
    kind: "frame",
    metadata: {
      tracking: header.tracking,
      transform: header.transform,
      native_image: header.image,
    },
    sections: [
      {
        name: "rgb",
        format: "jpeg",
        offset: 0,
        length: JPEG.length,
        shape: [60, 80],
      },
      {
        name: "raw_depth",
        format: "f32le",
        offset: JPEG.length,
        length: 1200,
        shape: [15, 20],
      },
      {
        name: "raw_confidence",
        format: "u8",
        offset: JPEG.length + 1200,
        length: 300,
        shape: [15, 20],
      },
    ],
  };
  const json = Buffer.from(JSON.stringify(version === 2 ? rich : header));
  const body = Buffer.alloc(4 + json.length + JPEG.length + 1500);
  body.writeUInt32LE(json.length, 0);
  json.copy(body, 4);
  JPEG.copy(body, 4 + json.length);
  for (let i = 0; i < 300; i++)
    body.writeFloatLE(2, 4 + json.length + JPEG.length + i * 4);
  body.fill(2, 4 + json.length + JPEG.length + 1200);
  return body;
}
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
