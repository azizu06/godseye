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
    /** Extra fake routes; return undefined to fall through. */
    extra?: (path: string, body: any) => unknown;
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
    const extra = opts.extra?.(path, body);
    if (extra !== undefined) return route.fulfill({ json: extra });
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

  await page
    .getByRole("button", { name: "Arm rover", exact: true })
    .first()
    .click();
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
  await page
    .getByRole("button", { name: "Arm rover", exact: true })
    .first()
    .click();
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
  // The spoken result is the checked card, never the model's claim that it is driving.
  await expect
    .poll(() => rover.calls.find((c) => c.path === "/voice/confirm")?.body)
    .toEqual({
      token: "token-1",
      text: "Drive to the backpack is on screen. Arm the rover first; confirming switches it to navigate. Say cancel to drop it.",
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

test("a spoken move is armed for and confirmed by a person, then reports the measured result", async ({
  page,
}) => {
  let polls = 0;
  const move = (
    status: string,
    achieved: number | null,
    text: string | null,
  ) => ({
    version: 1,
    move: {
      version: 1,
      move_id: "m-1",
      proposal_id: "p-move",
      status,
      reason: status === "running" ? null : "move_complete",
      direction: "forward",
      amount: 20,
      unit: "cm",
      label: "forward 20 cm (0.20 m)",
      quantity: "distance",
      requested: 0.2,
      requested_unit: "m",
      achieved,
      measured: achieved !== null,
      text,
    },
  });
  const rover = await fakeRover(page, {
    voice: [
      {
        version: 1,
        session_id: "nav-room",
        map_epoch: 1,
        status: "ok",
        question: "Move forward 20 centimeters",
        answer: "Moving forward now.",
        evidence: { objects: 0, changes: 0 },
        speech: null,
        actions: [
          {
            id: "srv-move",
            name: "propose_move",
            args: { direction: "forward", amount: 20, unit: "cm" },
          },
        ],
        confirm: "token-move",
      },
    ],
    propose: (body) => ({
      version: 1,
      proposal_id: "p-move",
      action_id: body.action.id,
      name: "propose_move",
      kind: "move",
      session_id: body.session_id,
      map_epoch: body.map_epoch,
      status: "ready",
      reason: null,
      message: null,
      move: {
        direction: "forward",
        amount: 20,
        unit: "cm",
        label: "forward 20 cm (0.20 m)",
        quantity: "distance",
        requested: 0.2,
        requested_unit: "m",
        limits:
          "one forward move of 5 to 50 centimeters, or one left or right turn of 10 to 90 degrees",
      },
      expires_in_s: 30,
      execution: {
        available: false,
        reason: "move_arm_required",
        message:
          "Arm the rover to confirm; the dashboard switches it to Standard for this one move.",
      },
      hardware_verified: false,
    }),
    extra: (path) => {
      if (path === "/nav/move") {
        polls += 1;
        return polls < 3
          ? move("running", polls === 1 ? null : 0.08, null)
          : move(
              "completed",
              0.21,
              "Moved forward 0.21 m of the requested 20 cm, measured by phone tracking.",
            );
      }
      if (path === "/nav/move/speak")
        return { version: 1, move_id: "m-1", speech: { status: "error" } };
      if (path === "/nav/confirm")
        return {
          version: 1,
          proposal_id: "p-move",
          move: move("running", null, null).move,
        };
      return undefined;
    },
  });
  await closeWorkspace(page);
  await page.getByRole("button", { name: "Ask Scout", exact: true }).click();
  await page.waitForTimeout(450);
  await page.getByRole("button", { name: "Stop and send" }).click();
  const card = page.getByTestId("nav-proposal");
  await expect(card).toContainText("Move forward 20 cm");
  await expect(card).toContainText("forward 20 cm (0.20 m)");
  await expect(card).toContainText("No obstacle check");
  const confirm = card.getByRole("button", { name: "Confirm move" });
  await expect(confirm).toBeDisabled();
  // Speech only put it on screen: nothing armed, selected or moved.
  await expect
    .poll(() => rover.calls.find((c) => c.path === "/voice/confirm")?.body)
    .toEqual({
      token: "token-move",
      text: "Move forward 20 centimeters is ready. Say go to arm for this move, or cancel.",
    });
  for (const path of ["/arm", "/mode", "/nav/confirm", "/manual"])
    expect(count(rover, path)).toBe(0);

  await card.getByRole("button", { name: "Arm for this move" }).click();
  await expect.poll(() => rover.armed).toBe(true);
  expect(rover.mode).toBe("manual");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(card.getByTestId("nav-proposal-result")).toHaveText(
    "Moved forward 0.21 m of the requested 20 cm, measured by phone tracking.",
  );
  expect(count(rover, "/nav/confirm")).toBe(1);
  expect(rover.calls.find((c) => c.path === "/nav/confirm")!.body).toEqual({
    proposal_id: "p-move",
    session_id: "nav-room",
    map_epoch: 1,
  });
  // The measured sentence is spoken once, only after the move ended; never a drive or steer command.
  await expect.poll(() => count(rover, "/nav/move/speak")).toBe(1);
  expect(rover.calls.find((c) => c.path === "/nav/move/speak")!.body).toEqual({
    move_id: "m-1",
  });
  expect(polls).toBeGreaterThanOrEqual(3);
  for (const path of ["/manual", "/goal"]) expect(count(rover, path)).toBe(0);
});

// A spoken "go" or "cancel" as the backend returns it: recognized without the answer model.
const spoken = (command: "confirm" | "cancel", token: string) => ({
  version: 1,
  session_id: "nav-room",
  map_epoch: 1,
  status: "ok",
  question: command === "confirm" ? "Go." : "Cancel.",
  answer: null,
  evidence: { objects: 0, changes: 0 },
  speech: null,
  command,
  actions: [],
  confirm: token,
});
async function say(page: Page) {
  const ask = page.getByRole("button", { name: "Ask Scout", exact: true });
  await expect(ask).toBeEnabled();
  await ask.click();
  await page.waitForTimeout(450);
  await page.getByRole("button", { name: "Stop and send" }).click();
}
const spokenText = (
  rover: { calls: { path: string; body: any }[] },
  token: string,
) =>
  rover.calls.find((c) => c.path === "/voice/confirm" && c.body.token === token)
    ?.body.text;

test("a spoken move is armed for and confirmed by voice, one prompted step at a time", async ({
  page,
}) => {
  const rover = await fakeRover(page, {
    voice: [
      {
        version: 1,
        session_id: "nav-room",
        map_epoch: 1,
        status: "ok",
        question: "Move forward 20 centimeters",
        answer: "Moving forward now.",
        evidence: { objects: 0, changes: 0 },
        speech: null,
        actions: [
          {
            id: "srv-move",
            name: "propose_move",
            args: { direction: "forward", amount: 20, unit: "cm" },
          },
        ],
        confirm: "t-offer",
      },
      spoken("confirm", "t-arm"),
      spoken("confirm", "t-go"),
    ],
    propose: (body) => ({
      version: 1,
      proposal_id: "p-move",
      action_id: body.action.id,
      kind: "move",
      session_id: body.session_id,
      map_epoch: body.map_epoch,
      status: "ready",
      reason: null,
      message: null,
      move: { direction: "forward", label: "forward 20 cm (0.20 m)" },
      expires_in_s: 30,
      execution: {
        available: false,
        reason: "move_arm_required",
        message: "Arm the rover to confirm.",
      },
    }),
    extra: (path) => {
      if (path === "/nav/move")
        return {
          version: 1,
          move: {
            move_id: "m-1",
            proposal_id: "p-move",
            status: "running",
            requested_unit: "m",
            achieved: null,
            text: null,
          },
        };
      if (path === "/nav/confirm")
        return { version: 1, proposal_id: "p-move", move: null };
      return undefined;
    },
  });
  await closeWorkspace(page);
  await say(page);
  const card = page.getByTestId("nav-proposal");
  await expect(card).toContainText("Move forward 20 cm");
  await expect
    .poll(() => spokenText(rover, "t-offer"))
    .toBe(
      "Move forward 20 centimeters is ready. Say go to arm for this move, or cancel.",
    );
  for (const path of ["/arm", "/mode", "/nav/confirm"])
    expect(count(rover, path)).toBe(0);

  // The first "go" presses only "Arm for this move": Standard, then arm. Nothing is confirmed.
  await say(page);
  await expect
    .poll(() => spokenText(rover, "t-arm"))
    .toBe("Armed in Standard for this move. Say go to move, or cancel.");
  expect(rover.armed).toBe(true);
  expect(rover.mode).toBe("manual");
  expect(count(rover, "/arm")).toBe(1);
  expect(count(rover, "/nav/confirm")).toBe(0);
  await expect(
    card.getByRole("button", { name: "Confirm move" }),
  ).toBeEnabled();

  // The second "go" presses Confirm move: the same single /nav/confirm as a click.
  await say(page);
  await expect
    .poll(() => spokenText(rover, "t-go"))
    .toBe("Confirmed. Moving now. Say stop to stop.");
  expect(count(rover, "/nav/confirm")).toBe(1);
  expect(rover.calls.find((c) => c.path === "/nav/confirm")!.body).toEqual({
    proposal_id: "p-move",
    session_id: "nav-room",
    map_epoch: 1,
  });
  expect(count(rover, "/arm")).toBe(1);
  for (const path of ["/manual", "/goal"]) expect(count(rover, path)).toBe(0);
});

test("spoken Explore selects the mode, then arms only after its explicit spoken prompt", async ({
  page,
}) => {
  const state: { rover?: { mode: string; armed: boolean } } = {};
  const rover = await fakeRover(page, {
    voice: [
      {
        version: 1,
        session_id: "nav-room",
        map_epoch: 1,
        status: "ok",
        question: "Explore the area",
        answer: "Exploring now.",
        evidence: { objects: 0, changes: 0 },
        speech: null,
        actions: [{ id: "srv-x", name: "propose_exploration", args: {} }],
        confirm: "t-offer",
      },
      spoken("confirm", "t-select"),
      spoken("confirm", "t-arm"),
    ],
    extra: (path, body) => {
      if (path !== "/nav/confirm") return undefined;
      // Like the backend: selecting Explore stops and disarms; it never arms.
      state.rover!.mode = "explore";
      state.rover!.armed = false;
      return {
        version: 1,
        mode: "explore",
        armed: false,
        next: "arm",
        proposal_id: body.proposal_id,
      };
    },
  });
  state.rover = rover;
  await closeWorkspace(page);
  await say(page);
  await expect
    .poll(() => spokenText(rover, "t-offer"))
    .toBe("Explore is ready. Say go to select Explore mode, or cancel.");
  await say(page);
  await expect
    .poll(() => spokenText(rover, "t-select"))
    .toBe(
      "Explore mode is selected and the rover is disarmed. Say go to arm and start exploring, or cancel.",
    );
  expect(count(rover, "/nav/confirm")).toBe(1);
  expect(count(rover, "/arm")).toBe(0);
  expect(rover.armed).toBe(false);
  await expect(page.getByTestId("nav-proposal-result")).toContainText(
    "Say go, or press Arm",
  );

  // Only this later "go" arms, with the same request as Rover controls' Arm button.
  await say(page);
  await expect
    .poll(() => spokenText(rover, "t-arm"))
    .toBe(
      "Arm requested. Explore starts when the rover is ready. Say stop to stop.",
    );
  expect(count(rover, "/arm")).toBe(1);
  expect(count(rover, "/nav/confirm")).toBe(1);
  expect(rover.armed).toBe(true);
});

test("a spoken go with two cards on screen does nothing, and cancel drops both", async ({
  page,
}) => {
  const rover = await fakeRover(page, {
    voice: [spoken("confirm", "t-go"), spoken("cancel", "t-cancel")],
  });
  await closeWorkspace(page);
  await offer(page, "propose_exploration", "a1");
  await offer(page, "propose_navigation", "a2", "nav-room", {
    target: "point",
    x: 0.6,
    z: 0.7,
  });
  await expect(page.getByTestId("nav-proposal")).toHaveCount(2);
  await expect(
    page.getByRole("button", { name: "Select Explore" }),
  ).toBeEnabled();
  await say(page);
  await expect
    .poll(() => spokenText(rover, "t-go"))
    .toBe(
      "More than one suggestion is on screen. Press the one you want, or say cancel. Nothing moved.",
    );
  for (const path of ["/nav/confirm", "/arm", "/mode"])
    expect(count(rover, path)).toBe(0);
  await say(page);
  await expect
    .poll(() => spokenText(rover, "t-cancel"))
    .toBe("Cancelled 2 suggestions. Nothing moved.");
  await expect(page.getByTestId("nav-proposal")).toHaveCount(0);
  expect(count(rover, "/nav/cancel")).toBe(2);
  expect(count(rover, "/nav/confirm")).toBe(0);
});
