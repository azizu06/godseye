import { test, expect, type Page } from "@playwright/test";
import {
  observedFeed,
  panel,
  workspaceAction,
  closeWorkspace,
} from "./helpers";

// A fake backend on an isolated port: no rover, phone, provider or real backend is used.
async function fakeRover(
  page: Page,
  opts: {
    car?: string;
    propose?: (body: any) => unknown;
    voice?: object[];
  } = {},
) {
  if (opts.voice) await fakeMicrophone(page);
  const state = {
    mode: "manual",
    armed: false,
    session: "nav-room",
    car: opts.car ?? "ok",
    calls: [] as { path: string; body: any }[],
  };
  await page.routeWebSocket("ws://localhost:9876/live", (ws) => {
    const update = () => {
      ws.send(
        JSON.stringify({
          version: 1,
          type: "objects",
          session_id: state.session,
          map_epoch: 1,
          objects: [],
        }),
      );
      ws.send(
        JSON.stringify({
          version: 1,
          type: "health",
          phone: "ok",
          car: state.car,
          detector: "ok",
          pose_age_ms: 1,
          mode: state.mode,
          armed: state.armed,
          stop_reason: null,
        }),
      );
      ws.send(
        JSON.stringify({
          version: 1,
          type: "pose",
          position: [0, 0.16, 0],
          yaw_rad: 0,
          tracking: "normal",
        }),
      );
    };
    update();
    const id = setInterval(update, 100);
    ws.onClose(() => clearInterval(id));
  });
  await page.route("http://localhost:9876/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: any = null;
    try {
      body = route.request().postDataJSON();
    } catch {
      /* the voice clip is audio, not JSON */
    }
    state.calls.push({ path, body });
    if (path === "/voice")
      return route.fulfill({
        json: { version: 1, status: opts.voice ? "ready" : "unavailable" },
      });
    if (path === "/voice/ask")
      return route.fulfill({
        json: opts.voice![count(state, "/voice/ask") - 1],
      });
    if (path === "/voice/confirm")
      return route.fulfill({
        json: { version: 1, status: "ok", speech: { status: "error" } },
      });
    if (path === "/nav/propose")
      return route.fulfill({ json: (opts.propose ?? ready)(body) });
    if (path === "/arm") state.armed = true;
    if (path === "/mode") {
      state.mode = body.mode;
      state.armed = false;
    }
    if (path === "/stop") state.armed = false;
    if (path === "/nav/confirm")
      return route.fulfill({
        json: {
          version: 1,
          goal: [0.6, 0.7],
          points: [],
          proposal_id: body.proposal_id,
        },
      });
    await route.fulfill({
      json: { version: 1, armed: state.armed, mode: state.mode },
    });
  });
  await observedFeed(page);
  await page.getByRole("button", { name: "2D", exact: true }).click();
  await workspaceAction(page, "Connection settings");
  await page.getByLabel("Enable REST commands").check();
  await page.getByLabel("Telemetry WebSocket").fill("ws://localhost:9876/live");
  await page.getByLabel("Backend API base").fill("http://localhost:9876");
  await page
    .getByRole("button", { name: "Connect source", exact: true })
    .click();
  await closeWorkspace(page);
  await panel(page, "Rover controls");
  await expect(page.locator(".operator-panel .state-pill").last()).toHaveText(
    "Disarmed",
  );
  return state;
}

// Fake microphone and recorder: no real capture, provider or audio device.
async function fakeMicrophone(page: Page) {
  await page.addInitScript(() => {
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
    Object.assign(window, { MediaRecorder: FakeRecorder });
    HTMLMediaElement.prototype.play = async function () {};
  });
}

function ready(body: any) {
  return {
    version: 1,
    proposal_id: "p-1",
    action_id: body.action.id,
    kind:
      body.action.name === "propose_exploration"
        ? "exploration"
        : "destination",
    session_id: body.session_id,
    map_epoch: body.map_epoch,
    status: "ready",
    reason: null,
    message: null,
    target: {
      kind: "object",
      class: "backpack",
      label: null,
      position: [0.8, 0.9],
    },
    destination: [0.6, 0.7],
    points: [
      [0, 0],
      [0.6, 0.7],
    ],
    length_m: 0.92,
    expires_in_s: 30,
    execution: { available: true, reason: null, message: null },
    hardware_verified: false,
  };
}

const offer = (
  page: Page,
  name: string,
  id = "a1",
  session = "nav-room",
  args: object = {},
) =>
  page.evaluate(
    ([name, id, session, args]) =>
      window.dispatchEvent(
        new CustomEvent("godseye:voice-actions", {
          detail: {
            session_id: session,
            map_epoch: 1,
            actions: [{ id, name, args }],
          },
        }),
      ),
    [name, id, session, args] as const,
  );
const count = (state: { calls: { path: string }[] }, path: string) =>
  state.calls.filter((c) => c.path === path).length;

test("a suggested destination needs a deliberate arm and is confirmed exactly once", async ({
  page,
}) => {
  const rover = await fakeRover(page);
  await offer(page, "propose_navigation", "a1", "nav-room", {
    target: "object",
    object_id: "db-1",
    class: "backpack",
  });
  const card = page.getByTestId("nav-proposal");
  await expect(card).toContainText("Drive to the backpack");
  await expect(card).toContainText("0.3 m from the backpack");
  const confirm = card.getByRole("button", { name: "Confirm drive" });
  await expect(confirm).toBeDisabled();
  await expect(card.getByTestId("nav-proposal-block")).toContainText(
    "Arm the rover first",
  );
  // Offering the same reply again never shows a second card or request.
  await offer(page, "propose_navigation", "a1", "nav-room", {
    target: "object",
    object_id: "db-1",
    class: "backpack",
  });
  await expect(page.getByTestId("nav-proposal")).toHaveCount(1);
  expect(count(rover, "/nav/propose")).toBe(1);
  expect(count(rover, "/arm")).toBe(0);

  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await expect(page.locator(".operator-panel .state-pill").last()).toHaveText(
    "Armed",
  );
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(card.getByTestId("nav-proposal-result")).toContainText(
    "Confirmed",
  );
  await expect(card.getByRole("button", { name: "Confirm drive" })).toHaveCount(
    0,
  );
  expect(count(rover, "/nav/confirm")).toBe(1);
  expect(rover.calls.find((c) => c.path === "/nav/confirm")!.body).toEqual({
    proposal_id: "p-1",
    session_id: "nav-room",
    map_epoch: 1,
  });
  // The same hand-off as click-to-navigate: navigate mode, re-arm, then the confirmation.
  const order = rover.calls
    .map((c) => c.path)
    .filter((p) => ["/mode", "/arm", "/nav/confirm"].includes(p));
  expect(order.slice(-3)).toEqual(["/mode", "/arm", "/nav/confirm"]);
});

test("unavailable suggestions explain why and offer map selection instead of guessing", async ({
  page,
}) => {
  const rover = await fakeRover(page, {
    car: "down",
    propose: (body) =>
      body.action.name === "propose_exploration"
        ? {
            ...ready(body),
            kind: "exploration",
            destination: null,
            execution: {
              available: false,
              reason: "car_down",
              autonomy_message:
                "This backend runs the logging car adapter; it cannot drive the rover.",
              message:
                "The car adapter reports down; the default adapter only logs. Autonomous driving is not physically validated: measured calibration, response curves and a supervised route are still missing.",
            },
          }
        : {
            ...ready(body),
            status: "unavailable",
            proposal_id: null,
            destination: null,
            reason: "target_ambiguous",
            message: "More than one matching object is in the map.",
            alternative:
              "Choose the destination yourself by clicking the floor on the map in Standard mode.",
          },
  });
  await offer(page, "propose_navigation", "a1", "nav-room", {
    target: "object",
    object_id: "db-1",
    class: "backpack",
  });
  const ambiguous = page.getByTestId("nav-proposal").first();
  await expect(ambiguous.getByTestId("nav-proposal-reason")).toContainText(
    "More than one matching object",
  );
  await expect(ambiguous).toContainText("clicking the floor on the map");
  await expect(
    ambiguous.getByRole("button", { name: "Confirm drive" }),
  ).toHaveCount(0);
  await offer(page, "propose_exploration", "a2");
  const explore = page.getByTestId("nav-proposal").nth(1);
  await expect(explore.getByTestId("nav-proposal-autonomy")).toContainText(
    "cannot drive the rover",
  );
  await expect(explore.getByTestId("nav-proposal-execution")).toContainText(
    "not physically validated",
  );
  await expect(
    explore.getByRole("button", { name: "Select Explore" }),
  ).toBeDisabled();
  expect(count(rover, "/nav/confirm")).toBe(0);
});

test("a stop suggestion only offers Stop, and a map change voids a pending suggestion", async ({
  page,
}) => {
  const rover = await fakeRover(page);
  await page.getByRole("button", { name: "Arm rover", exact: true }).click();
  await expect(page.locator(".operator-panel .state-pill").last()).toHaveText(
    "Armed",
  );
  const stops = count(rover, "/stop");
  await offer(page, "stop_navigation", "s1");
  const stop = page.getByTestId("nav-proposal").last();
  await expect(stop).toContainText("Stop the rover");
  await page.waitForTimeout(300);
  expect(count(rover, "/stop")).toBe(stops); // a suggestion never stops or moves by itself
  expect(rover.armed).toBe(true);
  await stop.getByRole("button", { name: "Stop rover" }).click();
  await expect.poll(() => count(rover, "/stop")).toBe(stops + 1);

  await offer(page, "propose_navigation", "a1", "nav-room", {
    target: "point",
    x: 0.6,
    z: 0.7,
  });
  const card = page.getByTestId("nav-proposal").last();
  await expect(
    card.getByRole("button", { name: "Confirm drive" }),
  ).toBeVisible();
  rover.session = "another-room";
  await expect(card.getByTestId("nav-proposal-reason")).toContainText(
    "The map changed",
  );
  await expect.poll(() => count(rover, "/nav/cancel")).toBe(1);
  expect(count(rover, "/nav/confirm")).toBe(0);
});

test("a spoken request becomes a confirmation card, never a drive command", async ({
  page,
}) => {
  const rover = await fakeRover(page, {
    voice: [
      {
        version: 1,
        session_id: "nav-room",
        map_epoch: 1,
        status: "ok",
        question: "Drive to the backpack",
        answer: "Driving to the backpack now.",
        evidence: { objects: 1, changes: 0 },
        speech: null,
        actions: [
          {
            id: "srv-1",
            name: "propose_navigation",
            args: { target: "object", object_id: "db-1", class: "backpack" },
          },
        ],
        confirm: "token-1",
      },
    ],
  });
  await closeWorkspace(page);
  await expect(
    page.getByRole("button", { name: "Ask Scout", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Ask Scout", exact: true }).click();
  await page.waitForTimeout(450);
  await page.getByRole("button", { name: "Stop and send" }).click();
  const card = page.getByTestId("nav-proposal");
  await expect(card).toContainText("Drive to the backpack");
  await expect(
    card.getByRole("button", { name: "Confirm drive" }),
  ).toBeDisabled();
  // The spoken result is what the dashboard did, never the model's claim that it is driving.
  await expect
    .poll(() => rover.calls.find((c) => c.path === "/voice/confirm")?.body)
    .toEqual({
      token: "token-1",
      text: "I put that suggestion on screen. Nothing moves unless you confirm it there.",
    });
  expect(rover.calls.find((c) => c.path === "/nav/propose")!.body).toEqual({
    session_id: "nav-room",
    map_epoch: 1,
    action: {
      id: "srv-1",
      name: "propose_navigation",
      args: { target: "object", object_id: "db-1", class: "backpack" },
    },
  });
  for (const path of ["/arm", "/mode", "/goal", "/nav/confirm", "/manual"])
    expect(count(rover, path)).toBe(0);
});
