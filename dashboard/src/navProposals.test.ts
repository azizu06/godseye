import { beforeEach, describe, expect, it } from "vitest";
import {
  armBlock,
  cardChanged,
  confirmBlock,
  currentOffers,
  navAction,
  offerNavigationActions,
  parseMove,
  parseProposal,
  registerCard,
  resetOffers,
  voiceCommand,
  voicePrompt,
  waitForCard,
  type CardContext,
  type CardHandle,
  type CardVoiceState,
  type NavAction,
  type NavActionName,
} from "./navProposals";

const map = { session_id: "s1", map_epoch: 1 };
const go = {
  id: "a1",
  name: "propose_navigation",
  args: { target: "object", object_id: "db-1", class: "backpack" },
};
const ready = {
  version: 1,
  proposal_id: "p1",
  kind: "destination",
  status: "ready",
  reason: null,
  message: null,
  target: {
    kind: "object",
    class: "backpack",
    label: null,
    position: [0.8, 0.9],
  },
  destination: [0.57, 0.72],
  length_m: 0.9,
  expires_in_s: 30,
  execution: { available: true, reason: null, message: null },
};
const ctx: CardContext = {
  mapKey: JSON.stringify(["s1", 1]),
  healthy: true,
  canDrive: true,
  mode: "manual",
  commands: true,
  now: 1000,
};

describe("offerNavigationActions", () => {
  beforeEach(resetOffers);
  it("keeps only navigation actions and handles each id once per map", () => {
    offerNavigationActions({
      ...map,
      actions: [go, { id: "v1", name: "set_view", args: {} }, "junk"],
    });
    offerNavigationActions({ ...map, actions: [go] }); // replayed reply
    expect(currentOffers().map((o) => o.action.id)).toEqual(["a1"]);
    offerNavigationActions({ session_id: "s2", map_epoch: 1, actions: [go] });
    expect(currentOffers()).toHaveLength(2);
  });
  it("ignores replies without a map or actions", () => {
    offerNavigationActions({ session_id: null, map_epoch: 1, actions: [go] });
    offerNavigationActions({ ...map, actions: undefined });
    expect(currentOffers()).toEqual([]);
  });
  it("drops malformed entries instead of repairing them", () => {
    for (const bad of [
      { ...go, extra: 1 },
      { ...go, id: "" },
      { ...go, args: null },
      { ...go, name: "arm" },
      { id: "a", name: "stop_navigation" },
    ])
      expect(navAction(bad)).toBeNull();
  });
});

describe("parseProposal", () => {
  it("reads a ready destination", () => {
    expect(parseProposal(ready)).toMatchObject({
      proposalId: "p1",
      destination: [0.57, 0.72],
      target: { className: "backpack", position: [0.8, 0.9] },
    });
  });
  it("keeps the prototype profile's caveats", () => {
    const parsed = parseProposal({
      ...ready,
      execution: {
        ...ready.execution,
        autonomy_warnings: ["Uncalibrated prototype: estimated chassis.", 7],
      },
    });
    expect(parsed!.execution.warnings).toEqual([
      "Uncalibrated prototype: estimated chassis.",
    ]);
    expect(parseProposal(ready)!.execution.warnings).toEqual([]);
  });
  it("rejects a ready proposal it could not confirm", () => {
    expect(parseProposal({ ...ready, proposal_id: null })).toBeNull();
    expect(parseProposal({ ...ready, destination: null })).toBeNull();
    expect(parseProposal({ ...ready, version: 2 })).toBeNull();
    expect(parseProposal({ ...ready, execution: undefined })).toBeNull();
  });
  it("keeps the backend's explanation of an unavailable one", () => {
    const parsed = parseProposal({
      ...ready,
      status: "unavailable",
      proposal_id: null,
      destination: null,
      reason: "target_ambiguous",
      message: "More than one matching object is in the map.",
      alternative: "Choose the destination yourself by clicking the floor.",
    });
    expect(parsed).toMatchObject({
      proposalId: null,
      reason: "target_ambiguous",
      alternative: expect.stringContaining("clicking"),
    });
  });
});

describe("confirmBlock", () => {
  const offer = { key: "k", mapKey: ctx.mapKey!, action: navAction(go)! };
  const proposal = parseProposal(ready)!;
  it("allows a confirmation only when ready, current, healthy and armed", () => {
    expect(confirmBlock(offer, proposal, 0, ctx)).toBeNull();
  });
  it("voids the card on a map change, expiry or unavailable proposal", () => {
    expect(
      confirmBlock(offer, proposal, 0, { ...ctx, mapKey: '["s2",1]' }),
    ).toMatchObject({ void: true });
    expect(
      confirmBlock(offer, proposal, 0, { ...ctx, now: 31_001 }),
    ).toMatchObject({ void: true });
    expect(
      confirmBlock(
        offer,
        { ...proposal, status: "unavailable", proposalId: null },
        0,
        ctx,
      ),
    ).toMatchObject({ void: true });
  });
  it("only disables it while health, arming or mode are not ready", () => {
    for (const change of [
      { healthy: false },
      { canDrive: false },
      { mode: "explore" },
      { commands: false },
    ])
      expect(
        confirmBlock(offer, proposal, 0, { ...ctx, ...change }),
      ).toMatchObject({ void: false });
  });
  it("lets exploration be confirmed disarmed, since it only selects the mode", () => {
    const explore = {
      ...proposal,
      kind: "exploration" as const,
      destination: null,
    };
    expect(
      confirmBlock(offer, explore, 0, { ...ctx, canDrive: false }),
    ).toBeNull();
  });
});

describe("voice reply parsing (dashboardActions.parseActions)", () => {
  it("accepts one resolved rover suggestion alone and rejects it beside view changes", async () => {
    const { parseActions } = await import("./dashboardActions");
    expect(parseActions([go])).toEqual([go]);
    expect(
      parseActions([{ id: "e", name: "propose_exploration", args: {} }]),
    ).toHaveLength(1);
    expect(
      parseActions([go, { id: "v", name: "set_view", args: { mode: "2d" } }]),
    ).toBeNull();
    for (const args of [
      { target: "object", ref: "o1" },
      { target: "object", object_id: "db-1", class: "backpack", speed: 1 },
      { target: "point", x: "1", z: 2 },
      { target: "point", x: 1, z: 99 },
      { target: "point", x: 1, z: 2, landmark: "Door!" },
      { target: "point", x: 1, z: 2, landmark: 7 },
      { target: "object", object_id: "db-1", class: "bag", landmark: "bag" },
    ])
      expect(parseActions([{ ...go, args }])).toBeNull();
  });

  it("accepts a backend-chosen point short of a named landmark and speaks its name", async () => {
    const { parseActions } = await import("./dashboardActions");
    const door: NavAction = {
      id: "l",
      name: "propose_navigation",
      args: { target: "point", x: 1, z: 2, landmark: "red door" },
    };
    expect(parseActions([door])).toEqual([door]);
    expect(
      parseActions([{ id: "p", name: "propose_landmark", args: { name: "door" } }]),
    ).toBeNull();
    const ready: CardVoiceState = {
      live: true,
      checking: false,
      step: "confirm",
      why: null,
      blocked: null,
    };
    expect(voicePrompt(door, ready)).toBe(
      "Drive to the red door is ready. Say go to drive, or cancel.",
    );
  });
});

describe("bounded moves", () => {
  const moveReady = {
    version: 1,
    proposal_id: "pm",
    kind: "move",
    status: "ready",
    reason: null,
    message: null,
    move: {
      direction: "forward",
      amount: 20,
      unit: "cm",
      label: "forward 20 cm (0.20 m)",
      limits: "one forward move of 5 to 50 centimeters",
    },
    expires_in_s: 30,
    execution: {
      available: false,
      reason: "move_arm_required",
      message: "Arm.",
    },
  };
  it("keeps only a stated distance or turn with its own unit", () => {
    const move = (args: object) =>
      navAction({ id: "m", name: "propose_move", args });
    expect(
      move({ direction: "forward", amount: 20, unit: "cm" }),
    ).not.toBeNull();
    expect(move({ direction: "left", amount: 30, unit: "deg" })).not.toBeNull();
    for (const args of [
      { direction: "forward", amount: 20 },
      { direction: "forward", amount: 20, unit: "deg" },
      { direction: "right", amount: 20, unit: "cm" },
      { direction: "backward", amount: 20, unit: "cm" },
      { direction: "forward", amount: "20", unit: "cm" },
      { direction: "forward", amount: 0, unit: "cm" },
      { direction: "forward", amount: 20, unit: "cm", speed: 1 },
    ])
      expect(move(args)).toBeNull();
  });
  it("reads a move proposal and refuses one without its summary", () => {
    expect(parseProposal(moveReady)?.move?.label).toBe(
      "forward 20 cm (0.20 m)",
    );
    expect(parseProposal({ ...moveReady, move: undefined })).toBeNull();
  });
  it("needs a deliberate arm outside Explore before a move can be confirmed", () => {
    const offer = {
      key: "k",
      mapKey: ctx.mapKey!,
      action: { id: "m", name: "propose_move" as const, args: {} },
    };
    const proposal = parseProposal(moveReady)!;
    expect(confirmBlock(offer, proposal, 1000, ctx)).toBeNull();
    expect(
      confirmBlock(offer, proposal, 1000, { ...ctx, canDrive: false })?.reason,
    ).toContain("Arm for this move");
    expect(
      confirmBlock(offer, proposal, 1000, { ...ctx, mode: "explore" })?.reason,
    ).toContain("Leave Explore");
  });
  it("reports only a finished move's measured sentence", () => {
    const base = {
      move_id: "m1",
      proposal_id: "pm",
      requested_unit: "m",
      achieved: 0.08,
      text: null,
    };
    expect(
      parseMove({ version: 1, move: { ...base, status: "running" } }),
    ).toMatchObject({
      status: "running",
      achieved: 0.08,
      text: null,
    });
    expect(
      parseMove({
        version: 1,
        move: { ...base, status: "stopped", text: "Stopped after 0.08 m." },
      }),
    ).toMatchObject({ status: "stopped", text: "Stopped after 0.08 m." });
    expect(
      parseMove({ version: 1, move: { ...base, status: "completed" } }),
    ).toBeNull();
    expect(parseMove({ version: 1, move: null })).toBeNull();
    expect(
      parseMove({ version: 1, move: { ...base, status: "teleported" } }),
    ).toBeNull();
  });
});

describe("spoken go and cancel (voiceCommand)", () => {
  beforeEach(resetOffers);
  const shown = JSON.stringify(["s1", 1]);
  const idle: CardVoiceState = {
    checking: false,
    live: false,
    step: null,
    blocked: null,
    why: null,
  };
  /** A card whose buttons only record presses; the real card presses its own confirm/arm buttons. */
  function card(
    key: string,
    state: Partial<CardVoiceState>,
    mapKey = shown,
    name: NavActionName = "propose_exploration",
  ) {
    const pressed: string[] = [];
    const handle: CardHandle = {
      key,
      mapKey,
      action: { id: key, name, args: {} },
      state: () => ({ ...idle, ...state }),
      go: async (step) => {
        pressed.push(step);
        return `pressed ${step}`;
      },
      cancel: () => {
        pressed.push("cancel");
        return "Cancelled. Nothing moved.";
      },
    };
    const unregister = registerCard(handle);
    return { pressed, unregister };
  }

  it("presses the one live card's own button, once", async () => {
    const one = card("c1", { live: true, step: "confirm" });
    expect(await voiceCommand("confirm", shown)).toBe("pressed confirm");
    expect(one.pressed).toEqual(["confirm"]);
  });

  it("presses the arm step only where the card itself offers it", async () => {
    const one = card("c1", { live: true, step: "arm" });
    expect(await voiceCommand("confirm", shown)).toBe("pressed arm");
    expect(one.pressed).toEqual(["arm"]);
  });

  it("does nothing with zero, two or only blocked cards, and says why", async () => {
    expect(await voiceCommand("confirm", shown)).toBe(
      "There is nothing to confirm. Nothing moved.",
    );
    const blocked = card("c1", {
      live: true,
      blocked: "Arm the rover first; confirming switches it to navigate.",
    });
    expect(await voiceCommand("confirm", shown)).toBe(
      "Arm the rover first; confirming switches it to navigate. Nothing moved.",
    );
    const second = card("c2", { live: true, step: "confirm" });
    expect(await voiceCommand("confirm", shown)).toContain(
      "More than one suggestion",
    );
    expect([...blocked.pressed, ...second.pressed]).toEqual([]);
  });

  it("never acts on an expired, voided, checking or other-map card", async () => {
    const expired = card("c1", {
      why: "This suggestion expired. Ask again.",
    });
    expect(await voiceCommand("confirm", shown)).toBe(
      "This suggestion expired. Ask again. Nothing moved.",
    );
    expired.unregister();
    const checking = card("c2", { checking: true });
    expect(await voiceCommand("confirm", shown)).toContain(
      "still being checked",
    );
    checking.unregister();
    const elsewhere = card(
      "c3",
      { live: true, step: "confirm" },
      JSON.stringify(["s2", 1]),
    );
    expect(await voiceCommand("confirm", shown)).toContain("different map");
    expect(await voiceCommand("confirm", null)).toContain("different map");
    expect([...expired.pressed, ...elsewhere.pressed]).toEqual([]);
  });

  it("cancel dismisses every live card on the shown map and nothing else", async () => {
    expect(await voiceCommand("cancel", shown)).toContain(
      "no suggestion to cancel",
    );
    const one = card("c1", { live: true, step: "confirm" });
    expect(await voiceCommand("cancel", shown)).toBe(
      "Cancelled. Nothing moved.",
    );
    const two = card("c2", { live: true, blocked: "Rover health is not ok." });
    const done = card("c3", { why: "That suggestion was already confirmed." });
    const elsewhere = card(
      "c4",
      { live: true, step: "confirm" },
      JSON.stringify(["s2", 1]),
    );
    expect(await voiceCommand("cancel", shown)).toBe(
      "Cancelled 2 suggestions. Nothing moved.",
    );
    expect(one.pressed).toEqual(["cancel", "cancel"]);
    expect(two.pressed).toEqual(["cancel"]);
    expect([...done.pressed, ...elsewhere.pressed]).toEqual([]);
  });

  it("waits for a new card's check, then speaks how to confirm it", async () => {
    let checking = true;
    const handle: CardHandle = {
      key: "k1",
      mapKey: shown,
      action: { id: "k1", name: "propose_exploration", args: {} },
      state: () =>
        checking
          ? { ...idle, checking: true }
          : { ...idle, live: true, step: "confirm" },
      go: async () => "",
      cancel: () => "",
    };
    const waiting = waitForCard("k1", 1000);
    registerCard(handle);
    checking = false;
    cardChanged();
    const found = await waiting;
    expect(found).toBe(handle);
    expect(voicePrompt(handle.action, found!.state())).toBe(
      "Explore is ready. Say go to select Explore mode, or cancel.",
    );
    expect(await waitForCard("missing", 10)).toBeNull();
  });

  it("words each prompt so the next spoken step is explicit", () => {
    const move: NavAction = {
      id: "m",
      name: "propose_move",
      args: { direction: "forward", amount: 50, unit: "cm" },
    };
    const turn: NavAction = {
      id: "t",
      name: "propose_move",
      args: { direction: "left", amount: 30, unit: "deg" },
    };
    const drive: NavAction = {
      id: "d",
      name: "propose_navigation",
      args: { target: "object", object_id: "db-1", class: "backpack" },
    };
    expect(voicePrompt(move, { ...idle, live: true, step: "arm" })).toBe(
      "Move forward 50 centimeters is ready. Say go to arm for this move, or cancel.",
    );
    expect(voicePrompt(turn, { ...idle, live: true, step: "confirm" })).toBe(
      "Turn left 30 degrees is ready. Say go to move, or cancel.",
    );
    expect(
      voicePrompt(drive, {
        ...idle,
        live: true,
        blocked: "Arm the rover first; confirming switches it to navigate.",
      }),
    ).toBe(
      "Drive to the backpack is on screen. Arm the rover first; confirming switches it to navigate. Say cancel to drop it.",
    );
    expect(
      voicePrompt(drive, {
        ...idle,
        why: "No rover-clear path reaches it on mapped floor.",
      }),
    ).toBe(
      "Drive to the backpack is not available. No rover-clear path reaches it on mapped floor. Nothing moved.",
    );
  });
});

describe("armBlock (Rover controls' Arm, shared with a spoken arm step)", () => {
  const base = {
    mission: {
      health: { phone: "ok", car: "ok", detector: "ok", mode: "explore" },
    },
    config: { commands: true, roverKey: "", serverPaired: false },
    autonomy: null,
    stale: false,
    pending: null,
    requiresStop: false,
  } as unknown as Parameters<typeof armBlock>[0];

  it("allows it exactly when the Arm button is enabled", () => {
    expect(armBlock(base)).toBeNull();
    expect(armBlock({ ...base, requiresStop: true })).toContain("Say stop");
    expect(armBlock({ ...base, pending: "/mode" })).toContain("in progress");
    expect(
      armBlock({ ...base, config: { ...base.config, commands: false } }),
    ).toContain("telemetry only");
    expect(armBlock({ ...base, stale: true })).toContain("healthy");
    const phone = {
      ...base,
      autonomy: { adapter: "iphone", ready: false },
    } as unknown as Parameters<typeof armBlock>[0];
    expect(armBlock(phone)).toContain("pairing key");
    expect(
      armBlock({ ...phone, config: { ...base.config, serverPaired: true } }),
    ).toBeNull();
  });
});
