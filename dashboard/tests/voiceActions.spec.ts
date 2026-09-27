import { test, expect, type Page } from "@playwright/test";
import { JPEG } from "./captureFixture";
import { closeWorkspace, workspaceAction } from "./helpers";

// Fake microphone, recorder and audio playback: no real capture, provider, audio
// device or car. Route mocks stand in for the backend's validated replies and its
// ElevenLabs-backed /voice/confirm. Browser speech is stubbed only to prove it is unused.
async function fakeMedia(page: Page) {
  await page.addInitScript(() => {
    const spoken: string[] = [];
    (window as unknown as { __spoken: string[] }).__spoken = spoken;
    const plays = { count: 0 };
    (window as unknown as { __plays: typeof plays }).__plays = plays;
    HTMLMediaElement.prototype.play = async function () {
      plays.count++;
      setTimeout(() => this.onended?.(new Event("ended")), 0);
    };
    HTMLMediaElement.prototype.pause = function () {};
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
// Strong evidence by default; `weak` is one low-confidence frame (objectDisplay.ts).
const object = (id: string, cls: string, x: number, weak = false) => ({
  id,
  class: cls,
  position: [x, 0.2, 1.5],
  confidence: weak ? 0.4 : 0.8,
  first_seen: now() - 60,
  last_seen: now() - 12,
  observations: weak ? 1 : 3,
  state: "present",
});
// A 0.1 s silent WAV standing in for Scout's ElevenLabs voice.
const WAV = (() => {
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + 3200, 4);
  header.write("WAVEfmt ", 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(16000, 24);
  header.writeUInt32LE(32000, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(3200, 40);
  return Buffer.concat([header, Buffer.alloc(3200)]).toString("base64");
})();
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
  confirm: `token-${(actions[0] as { id: string }).id}`,
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
          object("c2", "chair", 1, true),
          object("b1", "backpack", 0),
          object("p1", "person", 2, true),
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
  const confirmed: { token: string; text: string }[] = [];
  await page.route("http://localhost:9878/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/voice")
      return route.fulfill({ json: { version: 1, status: "ready" } });
    if (path === "/capture/detections.jpg")
      return route.fulfill({ body: JPEG, contentType: "image/jpeg" });
    if (path === "/voice/confirm") {
      confirmed.push(route.request().postDataJSON());
      return route.fulfill({
        json: {
          version: 1,
          status: "ok",
          speech: { status: "ready", mime: "audio/wav", data: WAV },
        },
      });
    }
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
  // Default evidence policy: the weak chair is hidden, the weak person shows as "Person?".
  await expect(page.locator(".scene-label")).toHaveCount(3);
  return { asked, confirmed };
}

const voice = (page: Page) =>
  page.getByRole("region", { name: "Ask Scout by voice" });
const result = (page: Page) => voice(page).getByRole("definition").last();
async function ask(page: Page) {
  await expect(
    page.getByRole("button", { name: "Ask Scout", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Ask Scout", exact: true }).click();
  await page.waitForTimeout(450);
  await page.getByRole("button", { name: "Stop and send" }).click();
}
const media = (page: Page) =>
  page.evaluate(() => {
    const w = window as unknown as {
      __spoken: string[];
      __plays: { count: number };
    };
    return { browserSpeech: w.__spoken, plays: w.__plays.count };
  });

test("spoken class filters compose with the evidence policy, keep raw data, speak real results and undo", async ({
  page,
}) => {
  const { confirmed } = await connect(page, [
    answer([act("a1", "filter_classes", { classes: ["backpack", "chair"] })]),
    answer([act("a2", "set_layer", { layer: "labels", visible: false })]),
    answer([act("a3", "undo")]),
    answer([act("a4", "focus_object", { object_id: "p1", class: "person" })]),
    answer([act("a5", "focus_object", { object_id: "b1", class: "backpack" })]),
    answer([act("a6", "set_view", { mode: "2d" })]),
    answer([act("a7", "undo")]),
  ]);
  const labels = page.locator(".scene-label");
  const chip = page.getByTestId("view-filter");

  await ask(page);
  await expect(result(page)).toHaveText("Showing only backpacks and chairs.");
  // The model's prose claim is never shown or spoken as the result; the applied
  // result is spoken once, afterwards, in Scout's own (ElevenLabs) voice.
  await expect(voice(page)).not.toContainText("already changed");
  await expect
    .poll(() => confirmed)
    .toEqual([
      { token: "token-a1", text: "Showing only backpacks and chairs." },
    ]);
  await expect.poll(async () => (await media(page)).plays).toBe(1);
  expect((await media(page)).browserSpeech).toEqual([]);
  // Filter then evidence policy: strong chair and backpack; weak chair stays hidden.
  await expect(labels).toHaveCount(2);
  await expect(labels.filter({ hasText: "Person" })).toHaveCount(0);
  await expect(chip).toContainText(
    "Showing Backpack, Chair · 1 hidden by filter, including 1 person",
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
  await workspaceAction(page, "Spatial memory");
  await expect(page.locator(".objects-panel .count-badge")).toHaveText("4");
  await closeWorkspace(page);

  await ask(page);
  await expect(result(page)).toHaveText("Labels hidden.");
  await expect(labels).toHaveCount(0);
  await expect(chip).toContainText("labels hidden");

  await ask(page);
  await expect(result(page)).toHaveText("Undid the last voice view change.");
  await expect(labels).toHaveCount(2);

  // A focused person stays discoverable even outside the class filter.
  await ask(page);
  await expect(result(page)).toHaveText(
    /^Focused on Person, last seen (1[2-9]|[2-5][0-9]) s ago\.$/,
  );
  await expect(page.locator(".scene-label.selected")).toContainText("Person?");
  await expect(chip).toContainText("Showing Backpack, Chair");
  await expect(chip).not.toContainText("hidden by filter");

  await ask(page);
  await expect(result(page)).toHaveText(
    /^Focused on Backpack, last seen (1[2-9]|[2-5][0-9]) s ago\.$/,
  );
  await expect(page.locator(".scene-label.selected")).toContainText("Backpack");
  await expect(labels).toHaveCount(2);

  await ask(page);
  await expect(result(page)).toHaveText("Switched to 2D.");
  await expect(
    page.getByRole("button", { name: "2D", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await ask(page);
  await expect(
    page.getByRole("button", { name: "3D", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(confirmed.map((c) => c.token)).toEqual(
    ["a1", "a2", "a3", "a4", "a5", "a6", "a7"].map((id) => `token-${id}`),
  );

  await chip.getByRole("button", { name: "Show all" }).click();
  await expect(chip).toHaveCount(0);
  await expect(labels).toHaveCount(3);
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
  const { asked, confirmed } = await connect(page, [
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
  await expect(page.locator(".scene-label")).toHaveCount(3);
  expect(asked.length).toBe(2);
  // Failures are spoken as failures; a cancelled reply speaks nothing.
  expect(confirmed.map((c) => c.text)).toEqual([
    "The map changed while Scout was answering. Nothing changed.",
    "That chair is no longer in this map. Nothing changed.",
  ]);
});
