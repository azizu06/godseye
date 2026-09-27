import { test, expect, type Page } from "@playwright/test";
import { workspaceAction, closeWorkspace } from "./helpers";

async function setup(
  page: Page,
  delayed = false,
  alreadyArmed = false,
  delayedStop = false,
) {
  let armed = alreadyArmed,
    mode = "explore",
    generation = 1,
    healthy = true;
  const sockets = new Set<any>();
  let open = true;
  let release = () => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  const requests: { path: string; body: any }[] = [];
  const health = () => ({
    version: 1,
    type: "health",
    phone: healthy ? "ok" : "down",
    car: "ok",
    detector: "ok",
    pose_age_ms: healthy ? 500 : null,
    mode,
    armed,
    stop_reason: null,
    motion_generation: generation,
  });
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    sockets.add(ws);
    const publish = () => {
      if (!open) return;
      ws.send(JSON.stringify(health()));
      ws.send(
        JSON.stringify({
          version: 1,
          type: "pose",
          position: [0, 0.2, 0],
          yaw_rad: 0,
          tracking: "normal",
        }),
      );
    };
    publish();
    const timer = setInterval(publish, 60);
    ws.onClose(() => {
      clearInterval(timer);
      sockets.delete(ws);
    });
  });
  await page.route("http://localhost:9876/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== "POST") {
      if (path === "/autonomy")
        return route.fulfill({
          json: {
            version: 1,
            adapter: "iphone",
            profile: "prototype",
            ready: true,
            blockers: [],
            manual_control: {
              forward: [0.05, 0.2],
              reverse: null,
              yaw: [0.05, 0.5],
              arcs: true,
            },
          },
        });
      return route.fulfill({ status: 404 });
    }
    const body = route.request().postDataJSON() ?? {};
    requests.push({ path, body });
    if (path === "/arm") {
      armed = true;
      generation++;
    }
    if (path === "/stop") {
      if (delayedStop) await gate;
      armed = false;
      generation++;
    }
    if (path === "/mode") {
      mode = body.mode;
      armed = false;
      generation++;
    }
    if (path === "/manual") {
      if (!armed || body.expected_generation !== generation)
        return route.fulfill({
          status: 409,
          json: { detail: "stale generation" },
        });
      if (body.takeover) {
        if (delayed) await gate;
        generation++;
        mode = "manual";
      }
      if (body.release) generation++;
    }
    return route.fulfill({ json: health() });
  });
  await page.goto("/?live=ws://localhost:9876/live");
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Rover pairing key").fill("a".repeat(32));
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  return {
    requests,
    release,
    restart: () => {
      generation = 1;
      armed = true;
      for (const ws of sockets) ws.close();
    },
    healthy: (v: boolean) => (healthy = v),
    disarm: () => {
      armed = false;
      generation++;
    },
    disconnect: () => (open = false),
  };
}
async function arm(page: Page) {
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Rover joystick" }),
  ).toBeEnabled();
}
async function press(page: Page) {
  const pad = page.getByRole("button", { name: "Rover joystick" });
  const b = (await pad.boundingBox())!;
  await page.mouse.move(b.x + b.width * 0.73, b.y + b.height * 0.27);
  await page.mouse.down();
  return b;
}
const moves = (r: { path: string; body: any }[]) =>
  r.filter(
    (x) => x.path === "/manual" && (x.body.v_mps || x.body.yaw_rate_rps),
  );

test("armed circular pad drives curves, holds center, releases and supports a fresh gesture", async ({
  page,
}) => {
  const f = await setup(page);
  const pad = page.getByRole("button", { name: "Rover joystick" });
  await expect(pad).toBeDisabled();
  await page.screenshot({ path: "/tmp/godseye-joystick-disabled.png" });
  await arm(page);
  const b = await press(page);
  await expect.poll(() => moves(f.requests).length).toBeGreaterThan(1);
  expect(moves(f.requests).at(-1)!.body).toMatchObject({
    v_mps: expect.any(Number),
    yaw_rate_rps: expect.any(Number),
  });
  expect(moves(f.requests).at(-1)!.body.v_mps).toBeGreaterThan(0);
  expect(moves(f.requests).at(-1)!.body.yaw_rate_rps).toBeLessThan(0);
  await page.screenshot({ path: "/tmp/godseye-joystick-armed.png" });
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await expect.poll(() => f.requests.at(-1)?.body.v_mps).toBe(0);
  const takeovers = () => f.requests.filter((r) => r.body.takeover).length;
  expect(takeovers()).toBe(1);
  await page.mouse.move(b.x + b.width * 0.25, b.y + b.height * 0.25);
  await expect
    .poll(() => f.requests.at(-1)?.body.yaw_rate_rps)
    .toBeGreaterThan(0);
  await page.mouse.up();
  await expect.poll(() => f.requests.at(-1)?.body.release).toBe(true);
  const n = f.requests.length;
  await page.waitForTimeout(250);
  expect(f.requests.length).toBe(n);
  await press(page);
  await expect.poll(takeovers).toBe(2);
  await expect.poll(() => moves(f.requests).length).toBeGreaterThan(3);
  await page.mouse.up();
  expect(f.requests.filter((r) => r.path === "/arm")).toHaveLength(1);
  expect(f.requests.filter((r) => r.path === "/mode")).toHaveLength(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "/tmp/godseye-joystick-mobile.png" });
});
for (const interrupt of [
  "blur",
  "disarm",
  "disconnect",
  "cancel",
  "lostcapture",
] as const)
  test(`joystick releases on ${interrupt}`, async ({ page }) => {
    const f = await setup(page);
    await arm(page);
    await press(page);
    await expect.poll(() => moves(f.requests).length).toBeGreaterThan(0);
    if (interrupt === "blur")
      await page.evaluate(() => window.dispatchEvent(new Event("blur")));
    if (interrupt === "disarm") f.disarm();
    if (interrupt === "disconnect") f.disconnect();
    if (interrupt === "cancel" || interrupt === "lostcapture")
      await page
        .getByRole("button", { name: "Rover joystick" })
        .evaluate(
          (el, name) =>
            el.dispatchEvent(
              new PointerEvent(
                name === "cancel" ? "pointercancel" : "lostpointercapture",
                { pointerId: 1, bubbles: true },
              ),
            ),
          interrupt,
        );
    await expect.poll(() => f.requests.some((r) => r.body.release)).toBe(true);
    const n = moves(f.requests).length;
    await page.waitForTimeout(260);
    expect(moves(f.requests).length).toBe(n);
    await page.mouse.up();
  });
test("release during takeover never starts later motion", async ({ page }) => {
  const f = await setup(page, true);
  await arm(page);
  await press(page);
  await expect.poll(() => f.requests.some((r) => r.body.takeover)).toBe(true);
  await page.mouse.up();
  f.release();
  await page.waitForTimeout(350);
  expect(moves(f.requests)).toHaveLength(0);
});

test("a new dashboard controls an already armed rover without Arm and blocks a pending Stop", async ({
  page,
}) => {
  const f = await setup(page, false, true, true);
  const pad = page.getByRole("button", { name: "Rover joystick" });
  await expect(pad).toBeEnabled();
  await press(page);
  await expect.poll(() => moves(f.requests).length).toBeGreaterThan(0);
  await page.mouse.up();
  expect(f.requests.filter((r) => r.path === "/arm")).toHaveLength(0);
  await page.getByRole("button", { name: "STOP ROVER", exact: true }).click();
  await expect(pad).toBeDisabled();
  await page.waitForTimeout(250);
  await expect(pad).toBeDisabled();
  const n = moves(f.requests).length;
  await page.keyboard.press("ArrowUp");
  expect(moves(f.requests).length).toBe(n);
  f.release();
});
test("keyboard takeover owns motion despite old pointer events", async ({
  page,
}) => {
  const f = await setup(page, false, true);
  await press(page);
  await expect.poll(() => moves(f.requests).length).toBeGreaterThan(0);
  await page.evaluate(() =>
    window.dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }),
    ),
  );
  await expect
    .poll(() => f.requests.filter((r) => r.body.takeover).length)
    .toBe(2);
  await expect.poll(() => moves(f.requests).at(-1)?.body.v_mps).toBe(0);
  const before = f.requests.filter((r) => r.body.release).length;
  await page.mouse.move(1300, 160);
  await page.mouse.up();
  await page.waitForTimeout(200);
  expect(f.requests.filter((r) => r.body.release).length).toBe(before);
  expect(moves(f.requests).at(-1)?.body.v_mps).toBe(0);
  await page.evaluate(() =>
    window.dispatchEvent(
      new KeyboardEvent("keyup", { key: "ArrowLeft", bubbles: true }),
    ),
  );
  await expect
    .poll(() => f.requests.filter((r) => r.body.release).length)
    .toBe(before + 1);
});
test("same endpoint restart clears old generation and permits a new explicit gesture", async ({
  page,
}) => {
  const f = await setup(page, false, true);
  await press(page);
  await expect.poll(() => moves(f.requests).length).toBeGreaterThan(0);
  await page.mouse.up();
  const old = f.requests.filter((r) => r.body.takeover).length;
  f.restart();
  const pad = page.getByRole("button", { name: "Rover joystick" });
  await expect(pad).toBeDisabled();
  await expect(pad).toBeEnabled();
  await press(page);
  await expect
    .poll(() => f.requests.filter((r) => r.body.takeover).length)
    .toBe(old + 1);
  await expect.poll(() => f.requests.at(-1)?.body.expected_generation).toBe(2);
  await page.mouse.up();
  expect(f.requests.filter((r) => r.path === "/arm")).toHaveLength(0);
});

test("hook unmount invalidates pending authority even without a pad-owned gesture", async ({
  page,
}) => {
  const f = await setup(page, true, true);
  await page.evaluate(async () => {
    const load = (path: string) => import(path);
    const { default: ReactDOM } = await load(
      "/node_modules/.vite/deps/react-dom_client.js",
    );
    const { createRoot } = ReactDOM;
    const { default: React } = await load("/node_modules/.vite/deps/react.js");
    const { createElement } = React;
    const { useMission } = await load("/src/useMission.ts");
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);
    (window as any).hookRoot = root;
    function Harness() {
      (window as any).hook = useMission();
      return null;
    }
    root.render(createElement(Harness));
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).hook?.connection))
    .toBe("connected");
  await page.evaluate(() => {
    const h = (window as any).hook;
    h.setConfig({ ...h.config, commands: true, roverKey: "a".repeat(32) });
  });
  await expect
    .poll(() => page.evaluate(() => (window as any).hook?.canJoystick))
    .toBe(true);
  await page.evaluate(() => (window as any).hook.beginDrive(0.1, 0.1));
  await expect.poll(() => f.requests.some((r) => r.body.takeover)).toBe(true);
  await page.evaluate(() => (window as any).hookRoot.unmount());
  f.release();
  await page.waitForTimeout(400);
  expect(moves(f.requests)).toHaveLength(0);
});
