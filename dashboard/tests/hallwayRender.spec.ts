import { expect, test, type Page } from "@playwright/test";
import { capture } from "./captureFixture";

// Public viewer transport only: a scripted /live WebSocket and /capture HTTP
// source on a dedicated port. No real backend, phone, rover or provider exists.
// The scene is read through three.js' devtools hook, which also works on the
// production bundle (`vite build` + `vite preview`).
const scope = { session_id: "surface-room", map_epoch: 1 };
const FLOOR_Y = -0.35;

type Packet = {
  cameraX?: number;
  /** Depth (m) over the body rectangle; the wall stays at 2 m. */
  body?: number;
  floor?: boolean;
};

function sections(body: Buffer) {
  const length = body.readUInt32LE(0);
  const header = JSON.parse(body.toString("utf8", 4, 4 + length));
  const at = (name: string) =>
    4 +
    length +
    header.sections.find((s: { name: string }) => s.name === name).offset;
  return { depth: at("raw_depth"), confidence: at("raw_confidence") };
}

/** A camera 35° pitched down at world Y 0 over a flat floor at FLOOR_Y. */
function floorPacket(frame: number) {
  const pitch = (-35 * Math.PI) / 180,
    c = Math.cos(pitch),
    s = Math.sin(pitch);
  const transform = [1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1];
  const body = capture(1, 2, frame, 0);
  const length = body.readUInt32LE(0);
  const header = JSON.parse(body.toString("utf8", 4, 4 + length));
  header.transform = transform;
  header.metadata.transform = transform;
  const json = Buffer.from(JSON.stringify(header));
  const out = Buffer.alloc(4 + json.length + body.length - 4 - length);
  out.writeUInt32LE(json.length, 0);
  json.copy(out, 4);
  body.copy(out, 4 + json.length, 4 + length);
  const { depth, confidence } = sections(out);
  for (let j = 0; j < 15; j++)
    for (let i = 0; i < 20; i++) {
      const u = ((i + 0.5) * 80) / 20,
        v = ((j + 0.5) * 60) / 15;
      const ray = [(u - 40) / 40, -(v - 30) / 40, -1];
      const worldY = c * ray[1] - s * ray[2];
      const d = worldY < 0 ? FLOOR_Y / worldY : Infinity;
      const ok = d > 0.05 && d <= 5;
      out.writeFloatLE(ok ? d : 0, depth + (j * 20 + i) * 4);
      out.writeUInt8(ok ? 2 : 0, confidence + j * 20 + i);
    }
  return out;
}

function packet(frame: number, { cameraX = 0, body, floor }: Packet) {
  if (floor) return floorPacket(frame);
  const out = capture(1, 2, frame, cameraX);
  if (body !== undefined) {
    const { depth } = sections(out);
    // Camera at (cameraX, 1, 0): this is a body centred at (cameraX, 1, -body).
    for (let y = 4; y < 11; y++)
      for (let x = 6; x < 14; x++)
        out.writeFloatLE(body, depth + (y * 20 + x) * 4);
  }
  return out;
}

const person = (position: number[], object_id: string) => ({
  class: "person",
  confidence: 0.82,
  box: [24, 16, 56, 44],
  position,
  depth_m: 1,
  object_id,
});
const detections = (frame: number, boxes: unknown[]) => ({
  version: 1,
  type: "detections",
  ...scope,
  frame_id: frame,
  t_capture: frame / 2,
  t_wall_ms: Date.now(),
  image: { width: 80, height: 60 },
  source: "backend_detector",
  classes: ["person"],
  detections: boxes,
});

const occupancy = (floor_y: number | undefined) => ({
  version: 1,
  type: "occupancy",
  ...scope,
  origin: [-1, -2],
  cell_m: 0.5,
  width: 4,
  height: 4,
  cells: Buffer.from(new Uint8Array(16).fill(1)).toString("base64"),
  floor_y,
});

/** Right-drag rotates the viewer camera down toward a side view of the floor. */
async function lowView(page: Page) {
  await page.getByRole("button", { name: "Frame scan", exact: true }).click();
  await page.mouse.move(720, 560);
  await page.mouse.down({ button: "right" });
  await page.mouse.move(720, 420, { steps: 8 });
  await page.mouse.up({ button: "right" });
}

async function sceneState(page: Page, mark = false) {
  return page.evaluate((mark) => {
    const scene = (window as any).__godseyeScenes.find((s: any) =>
      s.getObjectByName("floor-grid"),
    );
    if (!scene) return null;
    const world = (o: any) => {
      o.updateWorldMatrix(true, false);
      return [o.matrixWorld.elements[12], o.matrixWorld.elements[13]];
    };
    const markers: number[][] = [];
    let surfaceMinY = Infinity,
      foregroundFaces = 0,
      persistent: any = null;
    scene.traverse((o: any) => {
      if (o.name === "retained-person-marker") markers.push(world(o));
      const surface =
        o.isMesh && o.visible && o.parent?.name === "observed-color-surfaces";
      if (!surface) return;
      const g = o.geometry,
        p = g.attributes.position,
        index = g.index;
      if (!g.attributes.uv) persistent = g;
      const count = Math.min(g.drawRange.count, index.count);
      for (let i = 0; i < count; i += 3) {
        const ids = [0, 1, 2].map((j) => index.getX(i + j));
        for (const id of ids) surfaceMinY = Math.min(surfaceMinY, p.getY(id));
        if (ids.every((id) => p.getZ(id) > -1.3 && p.getZ(id) < -0.7))
          foregroundFaces++;
      }
    });
    const w = window as any;
    if (mark && persistent) {
      w.__persistentGeometry = persistent;
      w.__persistentPositions = persistent.attributes.position.array;
    }
    return {
      grid: world(scene.getObjectByName("floor-grid"))[1],
      rover: world(scene.getObjectByName("rover-phone-glyph") ?? scene)[1],
      markers,
      surfaceMinY,
      foregroundFaces,
      sameRetainedGeometry:
        !!persistent && w.__persistentGeometry === persistent,
      samePositionBuffer:
        !!persistent &&
        w.__persistentPositions === persistent.attributes.position.array,
      renders: w.__godseyeRenders as number,
    };
  }, mark);
}

test("retained people survive look-away and clear only on fresh depth; overlays sit on the confirmed floor", async ({
  page,
}) => {
  test.setTimeout(120000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const w = window as any;
    w.__godseyeScenes = [];
    w.__godseyeRenders = 0;
    const hook = new EventTarget();
    hook.addEventListener("observe", (event) => {
      const detail = (event as CustomEvent).detail;
      if (detail.isScene) w.__godseyeScenes.push(detail);
      if (typeof detail.render === "function" && detail.domElement) {
        const render = detail.render.bind(detail);
        detail.render = (...args: unknown[]) => {
          w.__godseyeRenders++;
          return render(...args);
        };
      }
    });
    w.__THREE_DEVTOOLS__ = hook;
  });
  let frame = 1,
    committed = 0,
    current: Packet = { floor: true };
  let send: (value: unknown) => void = () => {};
  const serve = async (next: number, view: Packet) => {
    frame = next;
    current = view;
    await expect.poll(() => committed, { timeout: 20000 }).toBe(next);
  };
  await page.routeWebSocket(/ws:\/\/(localhost|127\.0\.0\.1):8765\/.*/, (ws) =>
    ws.close(),
  );
  await page.route(/http:\/\/(localhost|127\.0\.0\.1):8765\/.*/, (route) =>
    route.fulfill({ status: 404 }),
  );
  await page.routeWebSocket("ws://localhost:9883/live", (ws) => {
    send = (value) => ws.send(JSON.stringify(value));
    const publish = () => {
      send({
        version: 1,
        type: "health",
        phone: "ok",
        car: "down",
        detector: "ok",
        pose_age_ms: 0,
        mode: "manual",
        armed: false,
        stop_reason: null,
        ...scope,
      });
      send({
        version: 1,
        type: "pose",
        ...scope,
        position: [0.4, 0, -0.2],
        yaw_rad: 0,
        tracking: "normal",
      });
    };
    publish();
    // No floor estimate yet: overlays stay at the AR origin height.
    send(occupancy(undefined));
    // Backend running-mean person records: represented by the retained markers.
    send({
      version: 1,
      type: "objects",
      ...scope,
      objects: ["a", "b"].map((id) => ({
        id: `backend-person-${id}`,
        class: "person",
        position: [id === "a" ? 0.2 : 3, 1, -1],
        confidence: 0.82,
        first_seen: 1,
        last_seen: Date.now() / 1000,
        observations: 3,
        state: "present",
      })),
    });
    const timer = setInterval(publish, 200);
    ws.onClose(() => clearInterval(timer));
  });
  await page.route("http://localhost:9883/**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/capture/surface.bin") {
      const tag = `"hall-${frame}"`;
      if (route.request().headers()["if-none-match"] === tag) {
        committed = frame;
        return route.fulfill({ status: 304, headers: { ETag: tag } });
      }
      return route.fulfill({
        body: packet(frame, current),
        contentType: "application/octet-stream",
        headers: {
          ETag: tag,
          "X-Capture-Age-Ms": "10",
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Expose-Headers": "ETag, X-Capture-Age-Ms",
        },
      });
    }
    if (url.pathname === "/capture/detections.jpg")
      return route.fulfill({ status: 404 });
    return route.fulfill({
      json: { version: 1, ...scope, objects: [], events: [], baselines: [] },
    });
  });
  await page.goto(
    "/?live=ws%3A%2F%2Flocalhost%3A9883%2Flive&api=http%3A%2F%2Flocalhost%3A9883",
  );
  await page.getByRole("button", { name: "3D", exact: true }).click();

  // 1. Floor: measured floor surface stays at world Y -0.35; overlays follow it.
  await serve(1, { floor: true });
  await expect(page.getByTestId("floor-status")).toHaveText(
    "Floor unknown · overlays at AR origin height",
  );
  await expect
    .poll(async () => (await sceneState(page))?.surfaceMinY)
    .toBeCloseTo(FLOOR_Y, 2);
  const unknown = (await sceneState(page))!;
  // Previous behavior: overlays assume Y = 0, 0.35 m above the measured floor.
  expect(unknown.grid).toBeCloseTo(-0.025, 5);
  expect(unknown.rover).toBeCloseTo(0.1, 5);
  await lowView(page);
  await page.screenshot({
    path: test.info().outputPath("floor-unknown-overlays-at-origin.png"),
  });
  send(occupancy(FLOOR_Y));
  await expect(page.getByTestId("floor-status")).toHaveText(
    "Floor Y -0.35 m · overlays on floor",
  );
  const floor = (await sceneState(page))!;
  // Measured geometry is untouched; only overlays move onto the floor.
  expect(floor.surfaceMinY).toBeCloseTo(unknown.surfaceMinY, 6);
  expect(floor.grid).toBeCloseTo(FLOOR_Y - 0.025, 5);
  expect(floor.rover).toBeCloseTo(FLOOR_Y + 0.1, 5);
  await page.waitForTimeout(300);
  await page.screenshot({
    path: test.info().outputPath("floor-aligned-overlays.png"),
  });

  // 2. Person A in view and person B off to the side, both detected LIVE.
  await serve(2, {});
  await serve(3, { body: 1 });
  const live = page.getByTestId("live-detection-label");
  const retained = page.getByTestId("retained-person-label");
  send(
    detections(3, [
      person([0, 1, -1], "backend-person-a"),
      person([3, 1, -1], "backend-person-b"),
    ]),
  );
  await expect(live).toHaveCount(2);
  await expect(retained).toHaveCount(0);
  // Running-mean backend boxes are not drawn a second time.
  await expect(page.locator(".scene-label", { hasText: "Person" })).toHaveCount(
    0,
  );

  // 3. Detector reports nobody: LIVE ends, one retained marker per person stays.
  const emptyAt = Date.now();
  send(detections(4, []));
  await expect(retained).toHaveCount(2);
  const retainedLatencyMs = Date.now() - emptyAt;
  await expect(live).toHaveCount(0);
  await expect(retained.first()).toContainText("not live");
  await page.screenshot({
    path: test.info().outputPath("retained-after-empty-frame.png"),
  });

  // 4. Camera looks away for two fresh views, plus time passes: nothing removed.
  await serve(4, { cameraX: 10 });
  await serve(5, { cameraX: 10 });
  await page.waitForTimeout(4000);
  expect((await sceneState(page))!.markers).toHaveLength(2);

  // 5. Camera returns: person A still stands there but is missed, then occluded.
  await serve(6, { body: 1 });
  await serve(7, { body: 0.5 });
  await serve(8, {});
  expect((await sceneState(page))!.markers).toHaveLength(2);
  const beforeClear = (await sceneState(page, true))!;
  expect(beforeClear.foregroundFaces).toBeGreaterThan(0);

  // 6. A second newer empty view proves A's old spot clear. B was never seen again.
  const clearAt = Date.now();
  await serve(9, {});
  await expect
    .poll(async () => (await sceneState(page))!.markers)
    .toEqual([[3, 1]]);
  const clearLatencyMs = Date.now() - clearAt;
  await expect(retained).toHaveCount(1);
  const after = (await sceneState(page))!;
  // The same proof retired the body mesh without recreating vertex GPU buffers.
  expect(after.foregroundFaces).toBe(0);
  expect(after.sameRetainedGeometry).toBe(true);
  expect(after.samePositionBuffer).toBe(true);
  expect(after.grid).toBeCloseTo(FLOOR_Y - 0.025, 5);
  await page.screenshot({
    path: test.info().outputPath("person-a-cleared-b-retained.png"),
  });
  const idle = after.renders;
  await page.waitForTimeout(1000);
  const idleRenders = (await sceneState(page))!.renders - idle;
  test.info().annotations.push({
    type: "measurement",
    description: JSON.stringify({
      retained_marker_after_empty_frame_ms: retainedLatencyMs,
      marker_removed_after_second_empty_capture_ms: clearLatencyMs,
      demand_renders_in_quiet_1s: idleRenders,
    }),
  });
  expect(errors).toEqual([]);
});
