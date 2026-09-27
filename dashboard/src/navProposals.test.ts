import { beforeEach, describe, expect, it } from "vitest";
import {
  confirmBlock,
  currentOffers,
  navAction,
  offerNavigationActions,
  parseMove,
  parseProposal,
  resetOffers,
  type CardContext,
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
    ])
      expect(parseActions([{ ...go, args }])).toBeNull();
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
