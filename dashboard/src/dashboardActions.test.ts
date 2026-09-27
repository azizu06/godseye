import { describe, expect, it } from "vitest";
import {
  DEFAULT_VIEW,
  parseActions,
  planActions,
  type PlanContext,
  type VoiceAction,
} from "./dashboardActions";
import type { WorldObject } from "./protocol";

const object = (id: string, cls: string, last_seen = 990): WorldObject => ({
  id,
  class: cls,
  position: [1, 0, 2],
  confidence: 0.8,
  first_seen: 900,
  last_seen,
  observations: 3,
  state: "present",
});
const action = (
  name: string,
  args: Record<string, unknown> = {},
  id = name,
): VoiceAction => ({ id, name, args }) as VoiceAction;
const context = (over: Partial<PlanContext> = {}): PlanContext => ({
  scope: '["room",1]',
  replyScope: '["room",1]',
  objects: [object("c1", "chair"), object("p1", "person", 5000)],
  controls: DEFAULT_VIEW,
  history: 0,
  hasGeometry: true,
  frame: { frameId: 7, capturedMs: 995_000, ageMs: 5000 },
  nowMs: 1_000_000,
  ...over,
});

describe("parseActions", () => {
  it("accepts only the bounded, typed action vocabulary", () => {
    expect(parseActions(undefined)).toEqual([]);
    expect(
      parseActions([
        { id: "a", name: "filter_classes", args: { classes: ["chair"] } },
        { id: "b", name: "set_view", args: { mode: "2d" } },
      ]),
    ).toHaveLength(2);
    for (const bad of [
      "x",
      [{ id: "a", name: "arm_car", args: {} }],
      [{ id: "a", name: "propose_navigation", args: { x: 1 } }],
      [{ id: "", name: "undo", args: {} }],
      [{ id: "a", name: "set_view", args: { mode: "4d" } }],
      [{ id: "a", name: "set_layer", args: { layer: "boxes" } }],
      [{ id: "a", name: "filter_classes", args: { classes: [] } }],
      [{ id: "a", name: "focus_object", args: { object_id: 3 } }],
      [{ id: "a", name: "frame_room", args: { url: "http://x" } }],
      Array.from({ length: 5 }, (_, i) => ({
        id: `${i}`,
        name: "frame_room",
        args: {},
      })),
    ])
      expect(parseActions(bad)).toBeNull();
  });
});

describe("planActions", () => {
  it("narrows display state and reports what it will show", () => {
    const plan = planActions(
      [
        action("filter_classes", { classes: ["chair", "backpack"] }),
        action("set_layer", { layer: "labels", visible: false }),
        action("set_view", { mode: "2d" }),
      ],
      context(),
    );
    expect(plan.ok).toBe(true);
    if (!plan.ok) return;
    expect(plan.controls).toEqual({
      view: "2d",
      classes: ["chair", "backpack"],
      boxes: true,
      labels: false,
    });
    expect(plan.reversible).toBe(true);
    expect(plan.lines.join(" ")).toBe(
      "Showing only chairs and backpacks. Labels hidden. Switched to 2D.",
    );
  });

  it("focuses a current object in 3D with an explicit last-seen age", () => {
    const plan = planActions(
      [action("focus_object", { object_id: "c1", class: "chair" })],
      context({ controls: { ...DEFAULT_VIEW, view: "2d" } }),
    );
    expect(plan.ok && plan.focus?.id).toBe("c1");
    expect(plan.ok && plan.controls.view).toBe("3d");
    expect(plan.ok && plan.lines).toEqual([
      "Focused on Chair, last seen 10 s ago.",
    ]);
    // An implausible phone clock is reported as unknown, never as a made-up age.
    const unknown = planActions(
      [action("open_evidence", { object_id: "p1", class: "person" })],
      context(),
    );
    expect(unknown.ok && unknown.evidence?.id).toBe("p1");
    expect(unknown.ok && unknown.lines).toEqual([
      "Opened Person evidence; last-seen time unknown.",
    ]);
  });

  it("changes nothing when any action cannot apply", () => {
    for (const [actions, over, text] of [
      [
        [action("filter_classes", { classes: ["chair"] })],
        { replyScope: '["room",2]' },
        "The map changed",
      ],
      [
        [
          action("set_view", { mode: "2d" }),
          action("focus_object", { object_id: "gone", class: "chair" }),
        ],
        {},
        "no longer in this map",
      ],
      [[action("frame_room")], { hasGeometry: false }, "No scan to frame"],
      [[action("save_camera_frame")], { frame: null }, "No phone camera frame"],
    ] as [VoiceAction[], Partial<PlanContext>, string][]) {
      const plan = planActions(actions, context(over));
      expect(plan.ok).toBe(false);
      expect(plan.ok ? "" : plan.text).toContain(text);
      expect(plan.ok ? "" : plan.text).toContain("Nothing changed");
    }
  });

  it("undoes only when there is a reversible voice change", () => {
    expect(planActions([action("undo")], context()).ok).toBe(false);
    const plan = planActions([action("undo")], context({ history: 2 }));
    expect(plan.ok && plan.undo).toBe(true);
    expect(plan.ok && plan.reversible).toBe(false);
  });

  it("captures are separate, non-reversible actions and never a fresh photo", () => {
    const plan = planActions(
      [action("download_view_snapshot"), action("save_camera_frame")],
      context(),
    );
    expect(plan.ok && plan.snapshot).toBe(true);
    expect(plan.ok && plan.saveFrame).toBe(true);
    expect(plan.ok && plan.reversible).toBe(false);
  });
});
