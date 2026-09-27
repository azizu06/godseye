import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
import { OperatorControls } from "./components";
import { emptyMission } from "./state";
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

it("preserves paired prototype arming via live backend validation after status integration", () => {
  const controller = {
    mission: emptyMission(),
    config: { ...defaultConfig, commands: true, roverKey: "test-pairing" },
    pending: null,
    stale: true,
    canDrive: false,
    requiresStop: false,
    autonomy: {
      version: 1,
      adapter: "iphone",
      profile: "prototype",
      ready: false,
      blockers: ["pose_stale"],
    },
    command: vi.fn(),
    steer: vi.fn(),
    releaseSteering: vi.fn(),
  } as unknown as MissionController;
  const render = () =>
    renderToStaticMarkup(createElement(OperatorControls, { controller }));
  const html = render();
  expect(html).toContain("Uncalibrated prototype");
  expect(html).toContain("Estimated geometry");
  expect(html).toContain("Click Arm to check current readiness and start");
  expect(
    html.match(/<button[^>]*aria-label="Arm rover"[^>]*>/)?.[0],
  ).not.toContain("disabled");
  controller.config = { ...controller.config, roverKey: undefined };
  expect(
    render().match(/<button[^>]*aria-label="Arm rover"[^>]*>/)?.[0],
  ).toContain("disabled");
  controller.requiresStop = true;
  controller.pending = "/arm";
  expect(
    render().match(/<button[^>]*aria-label="STOP ROVER"[^>]*>/)?.[0],
  ).not.toContain("disabled");
});
