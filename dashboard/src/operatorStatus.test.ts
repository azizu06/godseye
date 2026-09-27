import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
import { OperatorControls } from "./components";
import { emptyMission, reduceMessage } from "./state";
import { parseMessage } from "./protocol";
import { defaultConfig } from "./transport";
import type { MissionController } from "./useMission";

it("does not invent an armed state or selected mode before backend health arrives", () => {
  const controller = {
    mission: emptyMission(),
    config: defaultConfig,
    pending: null,
    stale: true,
    canDrive: false,
    requiresStop: false,
    command: vi.fn(),
    steer: vi.fn(),
    releaseSteering: vi.fn(),
  } as unknown as MissionController;
  const html = renderToStaticMarkup(
    createElement(OperatorControls, { controller }),
  );
  expect(html).toContain("Waiting for status");
  expect(html).not.toContain("Disarmed");
  expect(html).not.toContain('aria-pressed="true"');
  expect(html.match(/aria-pressed="false"/g)).toHaveLength(2);
});

function exploreController(reason?: unknown, armed = true): MissionController {
  const health = parseMessage({
    version: 1,
    type: "health",
    phone: "ok",
    car: "ok",
    detector: "ok",
    pose_age_ms: 0,
    mode: "explore",
    armed,
    stop_reason: null,
    ...(reason === undefined ? {} : { navigation_wait_reason: reason }),
  });
  if (!health) throw new Error("Invalid health fixture");
  return {
    mission: reduceMessage(emptyMission(), health),
    config: { ...defaultConfig, commands: true, roverKey: "test" },
    pending: null,
    stale: false,
    canDrive: false,
    requiresStop: armed,
    command: vi.fn(),
    steer: vi.fn(),
    releaseSteering: vi.fn(),
    autonomy: {
      adapter: "iphone",
      ready: true,
      blockers: [],
      auto_requested: true,
    },
  } as unknown as MissionController;
}

it.each([
  ["sensing_stale", "fresh depth"],
  ["detector_stale", "fresh detections"],
  ["start_blocked", "starting clearance"],
  ["no_feasible_step", "safe next move"],
  ["explore_complete", "reachable frontiers"],
  ["no_path", "clear route"],
  ["search_limit", "planning limit"],
])(
  "explains armed Explore wait %s in both control layouts",
  (reason, explanation) => {
    for (const compact of [false, true]) {
      const controller = exploreController(reason);
      const html = renderToStaticMarkup(
        createElement(OperatorControls, { controller, compact }),
      );
      expect(html).toContain("Explore waiting · armed");
      expect(html).toContain(explanation);
      expect(html).toContain('role="status"');
      expect(html).toContain('aria-label="STOP ROVER"');
      expect(html).toMatch(
        /<button[^>]*aria-label="STOP ROVER"(?![^>]*disabled)/,
      );
      expect(html).toContain('aria-pressed="true"');
      expect(html).not.toContain("Ready for explicit arming");
      expect(html).not.toContain(
        "Click Arm to check current readiness and start",
      );
    }
  },
);

it("keeps readiness blockers visible during an armed wait", () => {
  const controller = exploreController("sensing_stale");
  controller.autonomy = {
    ...controller.autonomy!,
    ready: false,
    blockers: ["rover_feedback_stale"],
  };
  const html = renderToStaticMarkup(
    createElement(OperatorControls, { controller }),
  );
  expect(html).toContain("fresh depth");
  expect(html).toContain("rover feedback stale");
});

it("clears the wait display on resumed or legacy health and after disarm", () => {
  for (const [reason, armed] of [
    [null, true],
    [undefined, true],
    ["no_path", false],
  ] as const) {
    const html = renderToStaticMarkup(
      createElement(OperatorControls, {
        controller: exploreController(reason, armed),
      }),
    );
    expect(html).not.toContain("Explore waiting · armed");
    expect(html).not.toContain("safe next move");
  }
});

it("shows an unfamiliar wait reason without claiming movement", () => {
  const html = renderToStaticMarkup(
    createElement(OperatorControls, {
      controller: exploreController("future_wait"),
    }),
  );
  expect(html).toContain("Explore waiting · armed");
  expect(html).toContain("future wait");
});

it("rejects malformed optional wait reasons while accepting older health", () => {
  expect(() => exploreController()).not.toThrow();
  expect(() => exploreController(null)).not.toThrow();
  for (const reason of [3, {}, []])
    expect(() => exploreController(reason)).toThrow("Invalid health fixture");
});
