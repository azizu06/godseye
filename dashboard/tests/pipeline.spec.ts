import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

test("binary phone RGB + depth travels through the real backend into rendered world points", async ({
  page,
}, testInfo) => {
  const backend = spawn(
    process.env.GODSEYE_PYTHON || "python3",
    ["tests/support/phone_backend.py"],
    {
      cwd: fileURLToPath(new URL("..", import.meta.url)),
    },
  );
  let phone: WebSocket | undefined;
  let poses: ReturnType<typeof setInterval> | undefined;
  let stderr = "";
  backend.stderr.on("data", (data) => {
    stderr += data;
  });
  try {
    const fixture = await new Promise<{ port: number; bundle: string }>(
      (resolve, reject) => {
        let output = "";
        const timer = setTimeout(
          () => reject(Error(`Backend startup timed out: ${stderr}`)),
          10000,
        );
        backend.on("error", (error) => {
          clearTimeout(timer);
          reject(error);
        });
        backend.on("exit", () => {
          clearTimeout(timer);
          reject(Error(`Backend exited: ${stderr}`));
        });
        backend.stdout.on("data", (data) => {
          output += data;
          if (output.includes("\n")) {
            clearTimeout(timer);
            resolve(JSON.parse(output.split("\n")[0]));
          }
        });
      },
    );
    const http = `http://127.0.0.1:${fixture.port}`;
    const ws = `ws://127.0.0.1:${fixture.port}`;
    await expect
      .poll(async () => {
        try {
          return (await fetch(`${http}/health`)).ok;
        } catch {
          return false;
        }
      })
      .toBe(true);
    await page.goto(`/?live=${encodeURIComponent(`${ws}/live`)}`);
    const status = page.getByRole("status", { name: "Point cloud status" });
    const canvas = page.getByRole("application", {
      name: "Interactive 3D viewport",
    });
    await expect(status).toContainText("Phone offline");
    await expect(canvas).toBeVisible();
    const render = () =>
      page.screenshot({ clip: { x: 250, y: 120, width: 900, height: 780 } });
    const empty = await render();
    phone = new WebSocket(`${ws}/phone`);
    await new Promise<void>((resolve, reject) => {
      phone!.onopen = () => resolve();
      phone!.onerror = () => reject(Error("Fixture phone could not connect"));
    });
    phone.send(
      JSON.stringify({
        version: 1,
        type: "hello",
        device: "synthetic-browser-test",
        session_id: "browser-pipeline",
        map_epoch: 1,
        supports_scene_depth: true,
        supports_mesh: false,
      }),
    );
    const binary = Buffer.from(fixture.bundle, "base64");
    const originalHeader = JSON.parse(
      binary.subarray(4, 4 + binary.readUInt32LE(0)).toString(),
    );
    const payload = binary.subarray(4 + binary.readUInt32LE(0));
    const started = performance.now();
    const captureTime = () => 10 + (performance.now() - started) / 1000;
    const pose = () =>
      phone!.send(
        JSON.stringify({
          ...originalHeader,
          type: "pose",
          t_capture: captureTime(),
          t_wall_ms: Date.now(),
          frame_id: Math.floor(captureTime() * 100),
        }),
      );
    pose();
    poses = setInterval(pose, 33);
    function frame(id: number, transform = originalHeader.transform) {
      // An encoded frame arrives 80 ms behind poses, as on the physical phone.
      const header = Buffer.from(
        JSON.stringify({
          ...originalHeader,
          frame_id: id,
          transform,
          t_capture: captureTime() - 0.08,
          t_wall_ms: Date.now(),
        }),
      );
      const prefix = Buffer.alloc(4);
      prefix.writeUInt32LE(header.length);
      phone!.send(Buffer.concat([prefix, header, payload]));
    }
    frame(1);
    await expect(status).toContainText("300 points");
    await expect.poll(async () => (await render()).equals(empty)).toBe(false);
    await canvas.press("f");
    const framed = await render();
    expect(framed.equals(empty)).toBe(false);
    const capture = await (await fetch(`${http}/capture/status`)).json();
    expect(capture.mapping.published).toBe(1);
    expect(capture.frame.metadata.frame_id).toBe(1);
    expect(capture.health.phone).toBe("ok");
    frame(2);
    await expect(status).toHaveAttribute("data-live", "true");
    await expect
      .poll(
        async () =>
          (await (await fetch(`${http}/capture/status`)).json()).mapping
            .published,
      )
      .toBe(2);
    async function geometry(anchors: unknown[]) {
      const header = Buffer.from(
        JSON.stringify({
          version: 2,
          type: "capture",
          kind: "geometry",
          session_id: "browser-pipeline",
          map_epoch: 1,
          frame_id: 100,
          t_capture: captureTime(),
          t_wall_ms: Date.now(),
          metadata: { anchors },
          sections: [],
        }),
      );
      const prefix = Buffer.alloc(4);
      prefix.writeUInt32LE(header.length);
      const result = await fetch(`${http}/capture/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: Buffer.concat([prefix, header]),
      });
      expect(result.ok).toBe(true);
    }
    await geometry([
      {
        id: "classified-wall",
        type: "plane",
        is_wall: true,
        alignment: 1,
        center: [0, 0, 0],
        extent: { width: 4, height: 3, rotation_y_rad: 0 },
        transform: [0, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, -1, 2, 3, 1],
      },
    ]);
    await expect(status).toContainText("1 walls");
    await expect(status).toContainText("0 points");
    await expect.poll(async () => (await render()).equals(framed)).toBe(false);
    const solidWall = await render();
    frame(3);
    await expect
      .poll(
        async () =>
          (await (await fetch(`${http}/capture/status`)).json()).mapping
            .wall_only,
      )
      .toBe(1);
    await expect(status).toContainText("0 points"); // Incoming wall samples consume no point capacity.
    const foreground = [...originalHeader.transform];
    foreground[12] += 0.3;
    frame(4, foreground);
    await expect(status).toContainText("300 points"); // Object detail 30 cm in front of the wall survives.
    await expect
      .poll(async () => (await render()).equals(solidWall))
      .toBe(false);
    const wallAndObject = await render();
    await page.screenshot({
      path: testInfo.outputPath("wall-and-foreground.png"),
    });
    clearInterval(poses);
    phone.close();
    await expect(status).toContainText("Phone offline");
    await expect(status).toContainText("300 points");
    expect((await render()).equals(wallAndObject)).toBe(true);
  } finally {
    clearInterval(poses);
    phone?.close();
    backend.kill("SIGTERM");
  }
});
