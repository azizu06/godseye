import { test, expect, type Page } from "@playwright/test";
import { JPEG } from "./captureFixture";
import { closeWorkspace, workspaceAction } from "./helpers";

// Fake microphone, recorder and on-device speech: no real capture, provider,
// audio device or car. The route mocks stand in for the backend's validated replies.
async function fakeMedia(page: Page) {
  await page.addInitScript(() => {
    const spoken: string[] = [];
    (window as unknown as { __spoken: string[] }).__spoken = spoken;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }),
      },
    });
    class FakeRecorder {
      state = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      start() {
        this.state = "recording";
      }
      stop() {
        this.state = "inactive";
        this.ondataavailable?.({ data: new Blob([new Uint8Array(4096)]) });
        setTimeout(() => this.onstop?.(), 0);
      }
    }
    class FakeUtterance {
      voice: unknown = null;
      onend: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(public text: string) {}
    }
    Object.assign(window, {
      MediaRecorder: FakeRecorder,
      SpeechSynthesisUtterance: FakeUtterance,
    });
    Object.defineProperty(window, "speechSynthesis", {
      configurable: true,
      value: {
        getVoices: () => [{ localService: true, lang: "en-US" }],
        speak: (u: FakeUtterance) => {
          spoken.push(u.text);
          setTimeout(() => u.onend?.(), 0);
        },
        cancel() {},
      },
    });
  });
}

const scope = { session_id: "room", map_epoch: 1 };
const now = () => Date.now() / 1000;
const object = (id: string, cls: string, x: number, ago = 12) => ({
  id,
  class: cls,
  position: [x, 0.2, 1.5],
  confidence: 0.8,
  first_seen: now() - 60,
  last_seen: now() - ago,
  observations: 3,
  state: "present",
});
const detection = (cls: string, x: number) => ({
  class: cls,
  confidence: 0.9,
  box: [x, 10, x + 12, 50],
  position: [x / 20, 0.2, 2],
  depth_m: 2,
  object_id: null,
});
const act = (id: string, name: string, args: object = {}) => ({
  id,
  name,
  args,
});
const answer = (actions: object[], over: object = {}) => ({
  version: 1,
  ...scope,
  status: "ok",
  question: "Show only backpacks and chairs",
  answer: "Done, I have already changed everything.",
  evidence: { objects: 4, changes: 0 },
  speech: null,
  actions,
  ...over,
});

async function connect(page: Page, replies: object[]) {
  await fakeMedia(page);
  await page.routeWebSocket("ws://localhost:8765/live", (ws) => ws.close());
  await page.route("http://localhost:8765/**", (r) =>
    r.fulfill({ status: 404 }),
  );
  await page.routeWebSocket("ws://localhost:9878/live", (ws) => {
    ws.send(
      JSON.stringify({
        version: 1,
        type: "objects",
        ...scope,
        objects: [
          object("c1", "chair", -1),
          object("c2", "chair", 1),
          object("b1", "backpack", 0),
          object("p1", "person", 2),
        ],
      }),
    );
    ws.send(
      JSON.stringify({
        version: 1,
        type: "detections",
        ...scope,
        frame_id: 7,
        t_capture: 7,
        t_wall_ms: Date.now(),
        image: { width: 80, height: 60 },
        source: "backend_detector",
        classes: ["person", "backpack", "chair", "bottle"],
        detections: [detection("person", 40), detection("chair", 4)],
      }),
    );
  });
  const asked: number[] = [];
  await page.route("http://localhost:9878/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/voice")
      return route.fulfill({ json: { version: 1, status: "ready" } });
    if (path === "/capture/detections.jpg")
      return route.fulfill({ body: JPEG, contentType: "image/jpeg" });
    if (path === "/voice/ask") {
      asked.push(1);
      return route.fulfill({ json: replies[asked.length - 1] });
    }
    return route.fulfill({ status: 404 });
  });
  await page.goto("/");
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9878/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9878");
  await page.getByLabel("Enable REST commands").check();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await expect(page.locator(".scene-label")).toHaveCount(4);
  return asked;
}

const voice = (page: Page) =>
  page.getByRole("region", { name: "Ask Scout by voice" });
const result = (page: Page) => voice(page).getByRole("definition").last();
async function ask(page: Page) {
  await page.getByRole("button", { name: "Ask Scout", exact: true }).click();
  await page.waitForTimeout(450);
  await page.getByRole("button", { name: "Stop and send" }).click();
}
const spoken = (page: Page) =>
  page.evaluate(() => (window as unknown as { __spoken: string[] }).__spoken);

test("voice actions narrow the view, report real results and undo", async ({
  page,
}) => {
  await connect(page, [
    answer([act("a1", "filter_classes", { classes: ["backpack", "chair"] })]),
    answer([act("a2", "set_layer", { layer: "labels", visible: false })]),
    answer([act("a3", "undo")]),
    answer([act("a4", "focus_object", { object_id: "b1", class: "backpack" })]),
    answer([act("a5", "set_view", { mode: "2d" })]),
    answer([act("a6", "undo")]),
  ]);
  const labels = page.locator(".scene-label");
  const chip = page.getByTestId("view-filter");

  await ask(page);
  await expect(result(page)).toHaveText("Showing only backpacks and chairs.");
  // The model's prose claim is never shown or spoken as the result.
  await expect(voice(page)).not.toContainText("already changed");
  expect(await spoken(page)).toEqual(["Showing only backpacks and chairs."]);
  await expect(labels).toHaveCount(3);
  await expect(chip).toContainText(
    "Showing Backpack, Chair · 1 hidden (1 person)",
  );
  // Raw evidence stays: the person box is hidden on the frame but still listed.
  const panel = page.getByTestId("detection-panel");
  await expect(panel.getByTestId("detection-box")).toHaveCount(1);
  await expect(panel.getByTestId("detection-filtered")).toContainText(
    "1 box hidden",
  );
  await expect(panel.locator(".detection-list li")).toHaveCount(2);
  await expect(page.getByTestId("live-detection-label")).toHaveText([
    "LIVE · Chair 90%",
  ]);

  await ask(page);
  await expect(result(page)).toHaveText("Labels hidden.");
  await expect(labels).toHaveCount(0);
  await expect(chip).toContainText("labels hidden");

  await ask(page);
  await expect(result(page)).toHaveText("Undid the last voice view change.");
  await expect(labels).toHaveCount(3);

  await ask(page);
  await expect(result(page)).toHaveText(
    /^Focused on Backpack, last seen (1[2-9]|[2-5][0-9]) s ago\.$/,
  );
  await expect(page.locator(".scene-label.selected")).toContainText("Backpack");

  await ask(page);
  await expect(result(page)).toHaveText("Switched to 2D.");
  await expect(
    page.getByRole("button", { name: "2D", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await ask(page);
  await expect(
    page.getByRole("button", { name: "3D", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");

  await chip.getByRole("button", { name: "Show all" }).click();
  await expect(chip).toHaveCount(0);
  await expect(labels).toHaveCount(4);
});

test("captures save the view and the latest received frame, never a new photo", async ({
  page,
}) => {
  await connect(page, [
    answer([
      act("s1", "download_view_snapshot"),
      act("s2", "save_camera_frame"),
    ]),
  ]);
  const downloads: string[] = [];
  const files: Promise<string | null>[] = [];
  page.on("download", (d) => {
    downloads.push(d.suggestedFilename());
    files.push(d.path());
  });
  await ask(page);
  await expect(result(page)).toContainText(
    "Saved an image of the 3D view without labels.",
  );
  await expect(result(page)).toContainText("Saved phone frame #7, captured");
  await expect(result(page)).toContainText(
    "It is the latest received frame, not a new photo.",
  );
  await expect.poll(() => downloads.length).toBe(2);
  expect(downloads[0]).toMatch(/^godseye-3d-view-.*\.png$/);
  expect(downloads[1]).toMatch(/^godseye-frame-7-.*\.jpg$/);
  const { readFileSync } = await import("node:fs");
  const [png, jpg] = await Promise.all(files);
  const view = readFileSync(png!);
  // A real rendered frame, not an empty or cleared drawing buffer.
  expect(view.subarray(1, 4).toString()).toBe("PNG");
  expect(view.length).toBeGreaterThan(5000);
  expect(readFileSync(jpg!).equals(JPEG)).toBe(true);
});

test("stale, missing or cancelled replies change nothing", async ({ page }) => {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  const asked = await connect(page, [
    answer([act("m1", "filter_classes", { classes: ["chair"] })], {
      map_epoch: 2,
    }),
    answer([
      act("m2", "set_view", { mode: "2d" }),
      act("m3", "focus_object", { object_id: "gone", class: "chair" }),
    ]),
  ]);
  await ask(page);
  await expect(result(page)).toHaveText(
    "The map changed while Scout was answering. Nothing changed.",
  );
  await ask(page);
  await expect(result(page)).toHaveText(
    "That chair is no longer in this map. Nothing changed.",
  );
  await expect(
    page.getByRole("button", { name: "3D", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("view-filter")).toHaveCount(0);

  // A reply that arrives after Cancel applies nothing.
  await page.unroute("http://localhost:9878/**");
  await page.route("http://localhost:9878/voice/ask", async (route) => {
    await gate;
    await route
      .fulfill({
        json: answer([act("late", "filter_classes", { classes: ["chair"] })]),
      })
      .catch(() => {});
  });
  await ask(page);
  await voice(page).getByRole("button", { name: "Cancel question" }).click();
  release();
  await page.waitForTimeout(400);
  await expect(page.getByTestId("view-filter")).toHaveCount(0);
  await expect(page.locator(".scene-label")).toHaveCount(4);
  expect(asked.length).toBe(2);
});
