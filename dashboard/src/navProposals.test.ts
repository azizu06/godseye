import { beforeEach, describe, expect, it } from "vitest";
import {
  confirmBlock,
  currentOffers,
  navAction,
  offerNavigationActions,
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
