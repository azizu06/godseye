import { test, expect, type Page } from "@playwright/test";
import { workspaceAction, closeWorkspace } from "./helpers";

// Fake microphone, recorder and playback: no real capture, provider or audio device is used.
async function fakeMedia(page: Page, denied = false) {
  await page.addInitScript((denied) => {
    const state = { gum: 0, stopped: 0, plays: 0, pauses: 0 };
    (window as unknown as { __voice: typeof state }).__voice = state;
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: async () => {
          state.gum++;
          if (denied) throw new DOMException("denied", "NotAllowedError");
          return { getTracks: () => [{ stop: () => state.stopped++ }] };
        },
      },
    });
    class FakeRecorder {
      state = "inactive";
      mimeType = "audio/webm;codecs=opus";
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      start() {
        this.state = "recording";
      }
      stop() {
        this.state = "inactive";
        this.ondataavailable?.({
          data: new Blob([new Uint8Array(4096)], { type: this.mimeType }),
        });
        setTimeout(() => this.onstop?.(), 0);
      }
    }
    (window as unknown as { MediaRecorder: unknown }).MediaRecorder =
      FakeRecorder;
    HTMLMediaElement.prototype.play = async function () {
      state.plays++;
    };
    HTMLMediaElement.prototype.pause = function () {
      state.pauses++;
    };
  }, denied);
}

const counters = (page: Page) =>
  page.evaluate(
    () =>
      (
        window as unknown as {
          __voice: {
            gum: number;
            stopped: number;
            plays: number;
            pauses: number;
          };
        }
      ).__voice,
  );

// A 0.1 s silent 16 kHz WAV, standing in for the backend's spoken reply.
function wav() {
  const pcm = 3200;
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + pcm, 4);
  header.write("WAVEfmt ", 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(16000, 24);
  header.writeUInt32LE(32000, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(pcm, 40);
  return Buffer.concat([header, Buffer.alloc(pcm)]).toString("base64");
}

const reply = {
  version: 1,
  session_id: "room",
  map_epoch: 1,
  status: "ok",
  question: "Where is the backpack?",
  answer: "The backpack was last seen 12 seconds ago near the far wall.",
  evidence: { objects: 3, changes: 1 },
  speech: { status: "ready", mime: "audio/wav", duration_s: 0.1, data: wav() },
};

async function connect(
  page: Page,
  voiceStatus: "ready" | "unavailable",
  denied = false,
) {
  await fakeMedia(page, denied);
  await page.routeWebSocket("ws://localhost:8765/live", (ws) => ws.close());
  await page.route("http://localhost:8765/**", (route) =>
    route.fulfill({ status: 404 }),
  );
  await page.routeWebSocket("ws://localhost:9878/live", () => {});
  await page.route("http://localhost:9878/**", (route) =>
    new URL(route.request().url()).pathname === "/voice"
      ? route.fulfill({ json: { version: 1, status: voiceStatus } })
      : route.fulfill({ status: 404 }),
  );
  await page.goto("/");
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9878/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9878");
  await page.getByLabel("Enable REST commands").check();
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
}

// Click once to start recording, wait, then click again to stop and send.
async function ask(page: Page, ms: number) {
  await page.getByRole("button", { name: "Ask Scout", exact: true }).click();
  const stop = page.getByRole("button", { name: "Stop and send" });
  await expect(stop).toHaveAttribute("aria-pressed", "true");
  await page.waitForTimeout(ms);
  await stop.click();
}

const voice = (page: Page) =>
  page.getByRole("region", { name: "Ask Scout by voice" });

test("unavailable backend shows a clear disabled state", async ({ page }) => {
  await connect(page, "unavailable");
  // The dock is a polite live region, not a second page-wide status role.
  await expect(voice(page).getByRole("status")).toHaveCount(0);
  await expect(voice(page).locator(".voice-status")).toHaveText(
    "Voice Q&A unavailable on this backend.",
  );
  await expect(
    page.getByRole("button", { name: "Ask Scout", exact: true }),
  ).toBeDisabled();
  expect((await counters(page)).gum).toBe(0);
});

test("click-to-talk question is answered, spoken and can be stopped", async ({
  page,
}) => {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  const uploads: { type: string | null; size: number }[] = [];
  await connect(page, "ready");
  await page.route("http://localhost:9878/voice/ask", async (route) => {
    uploads.push({
      type: route.request().headers()["content-type"] ?? null,
      size: route.request().postDataBuffer()?.length ?? 0,
    });
    await gate;
    await route.fulfill({ json: reply });
  });
  await expect(voice(page).locator(".voice-status")).toHaveText(
    "Click to ask Scout about what it has seen.",
  );
  // The microphone is untouched until the button is clicked.
  expect((await counters(page)).gum).toBe(0);
  await ask(page, 500);
  await expect(voice(page).locator(".voice-status")).toHaveText("Thinking…");
  // Released immediately after recording, before the answer returns.
  expect(await counters(page)).toMatchObject({ gum: 1, stopped: 1 });
  expect(uploads).toEqual([{ type: "audio/webm;codecs=opus", size: 4096 }]);
  release();
  await expect(voice(page).locator(".voice-status")).toHaveText("Speaking…");
  const exchange = page.getByRole("definition");
  await expect(exchange.first()).toHaveText("Where is the backpack?");
  await expect(exchange.last()).toHaveText(reply.answer);
  expect((await counters(page)).plays).toBe(1);
  await voice(page).getByRole("button", { name: "Stop speaking" }).click();
  await expect(voice(page).locator(".voice-status")).toHaveText("Stopped.");
  expect((await counters(page)).pauses).toBeGreaterThan(0);
  await expect(
    page.getByRole("button", { name: "Ask Scout", exact: true }),
  ).toBeEnabled();
  // The last answer stays visible as a caption beside the live evidence.
  await expect(exchange.last()).toHaveText(reply.answer);
});

test("thinking can be cancelled and a late answer is ignored", async ({
  page,
}) => {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  await connect(page, "ready");
  await page.route("http://localhost:9878/voice/ask", async (route) => {
    await gate;
    await route.fulfill({ json: reply }).catch(() => {});
  });
  await ask(page, 500);
  await expect(voice(page).locator(".voice-status")).toHaveText("Thinking…");
  await voice(page).getByRole("button", { name: "Cancel question" }).click();
  await expect(voice(page).locator(".voice-status")).toHaveText("Cancelled.");
  release();
  await page.waitForTimeout(300);
  await expect(page.getByRole("definition")).toHaveCount(0);
  expect((await counters(page)).plays).toBe(0);
});

test("short taps, provider errors and text-only replies are explained", async ({
  page,
}) => {
  const responses = [
    { status: 502, json: { detail: "Answer unavailable" } },
    { status: 200, json: { ...reply, speech: { status: "error" } } },
  ];
  let asked = 0;
  await connect(page, "ready");
  await page.route("http://localhost:9878/voice/ask", (route) =>
    route.fulfill(responses[asked++]),
  );
  await ask(page, 50);
  await expect(voice(page).locator(".voice-status")).toHaveText(
    "Too short. Click, speak, then click again to send.",
  );
  expect(asked).toBe(0);
  await ask(page, 500);
  await expect(voice(page).locator(".voice-status")).toHaveText(
    "Scout could not answer right now.",
  );
  await ask(page, 500);
  await expect(voice(page).locator(".voice-status")).toHaveText(
    "Spoken reply unavailable; the answer is shown.",
  );
  await expect(page.getByRole("definition").last()).toHaveText(reply.answer);
  expect((await counters(page)).plays).toBe(0);
});

test("cancelling while listening releases the microphone without sending", async ({
  page,
}) => {
  let asked = 0;
  await connect(page, "ready");
  await page.route("http://localhost:9878/voice/ask", (route) => {
    asked++;
    return route.fulfill({ json: reply });
  });
  await page.getByRole("button", { name: "Ask Scout", exact: true }).click();
  await expect(voice(page).locator(".voice-status")).toHaveText(
    "Listening… click again to send.",
  );
  await page.waitForTimeout(500);
  await voice(page).getByRole("button", { name: "Cancel question" }).click();
  await expect(voice(page).locator(".voice-status")).toHaveText("Cancelled.");
  await page.waitForTimeout(300);
  expect(asked).toBe(0);
  expect(await counters(page)).toMatchObject({ gum: 1, stopped: 1 });
  await expect(
    page.getByRole("button", { name: "Ask Scout", exact: true }),
  ).toBeEnabled();
});

test("recording stops and sends at the 15 s cap without a second click", async ({
  page,
}) => {
  const uploads: number[] = [];
  await page.clock.install();
  await connect(page, "ready");
  await page.route("http://localhost:9878/voice/ask", (route) => {
    uploads.push(route.request().postDataBuffer()?.length ?? 0);
    return route.fulfill({ json: { ...reply, speech: { status: "error" } } });
  });
  await page.getByRole("button", { name: "Ask Scout", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Stop and send" }),
  ).toBeVisible();
  await page.clock.runFor(14000);
  expect(uploads).toEqual([]);
  await page.clock.runFor(1100);
  await expect(voice(page).locator(".voice-status")).toHaveText(
    "Spoken reply unavailable; the answer is shown.",
  );
  expect(uploads).toEqual([4096]);
  expect(await counters(page)).toMatchObject({ gum: 1, stopped: 1 });
});

test("denied microphone permission is explained and nothing is sent", async ({
  page,
}) => {
  let asked = 0;
  await connect(page, "ready", true);
  await page.route("http://localhost:9878/voice/ask", (route) => {
    asked++;
    return route.fulfill({ json: reply });
  });
  await page.getByRole("button", { name: "Ask Scout", exact: true }).click();
  await expect(voice(page).locator(".voice-status")).toHaveText(
    "Microphone permission is needed to ask by voice.",
  );
  await expect(
    page.getByRole("button", { name: "Ask Scout", exact: true }),
  ).toBeEnabled();
  expect(asked).toBe(0);
  expect((await counters(page)).gum).toBe(1);
});

test("keyboard Enter toggles once per press to start and send", async ({
  page,
}) => {
  const uploads: number[] = [];
  await connect(page, "ready");
  await page.route("http://localhost:9878/voice/ask", (route) => {
    uploads.push(route.request().postDataBuffer()?.length ?? 0);
    return route.fulfill({ json: { ...reply, speech: { status: "error" } } });
  });
  await page.getByRole("button", { name: "Ask Scout", exact: true }).focus();
  await page.keyboard.press("Enter");
  const stop = page.getByRole("button", { name: "Stop and send" });
  await expect(stop).toHaveAttribute("aria-pressed", "true");
  // Releasing the key does not stop recording; only the next press does.
  await page.waitForTimeout(500);
  await expect(stop).toBeVisible();
  // Closing the workspace can restore focus to its launcher; refocus the toggle.
  await stop.focus();
  await page.keyboard.press("Enter");
  await expect(voice(page).locator(".voice-status")).toHaveText(
    "Spoken reply unavailable; the answer is shown.",
  );
  expect(uploads).toEqual([4096]);
  expect(await counters(page)).toMatchObject({ gum: 1, stopped: 1 });
});
