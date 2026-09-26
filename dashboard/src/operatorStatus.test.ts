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
