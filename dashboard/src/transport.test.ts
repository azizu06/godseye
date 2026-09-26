import { afterEach, describe, expect, it, vi } from "vitest";
import {
  defaultConfig,
  ManualController,
  sendCommand,
  validateConfig,
  initialConfig,
} from "./transport";
afterEach(() => vi.useRealTimers());
describe("held controls", () => {
  it("sends every 100ms while held and zero on release, then sends no more motion", async () => {
    vi.useFakeTimers();
    const sent: Record<string, number>[] = [];
    const controller = new ManualController(
      async (body) => {
        sent.push(body);
      },
      () => {},
    );
    controller.start(0.15, 0);
    await vi.advanceTimersByTimeAsync(250);
    expect(sent.filter((v) => v.v_mps === 0.15)).toHaveLength(3);
    controller.stop();
    await vi.advanceTimersByTimeAsync(500);
    expect(sent).toHaveLength(4);
    expect(sent.at(-1)).toEqual({ v_mps: 0, yaw_rate_rps: 0 });
  });
  it("never queues pulses behind a slow network", async () => {
    vi.useFakeTimers();
    const sent: Record<string, number>[] = [];
    const controller = new ManualController(
      (body) => {
        sent.push(body);
        return new Promise(() => {});
      },
      () => {},
    );
    controller.start(0.1, 0);
    await vi.advanceTimersByTimeAsync(700);
    expect(sent).toHaveLength(1);
    controller.stop();
    expect(sent.at(-1)).toEqual({ v_mps: 0, yaw_rate_rps: 0 });
  });
});
describe("feed selection", () => {
  it("starts telemetry-only and recovers a URL-selected backend without command permission", () => {
    expect(initialConfig("")).toMatchObject({
      source: "external",
      commands: false,
    });
    expect(
      initialConfig(
        "?live=wss%3A%2F%2Fscan.example%3A9876%2Flive&commands=true",
      ),
    ).toEqual({
      source: "external",
      commands: false,
      wsUrl: "wss://scan.example:9876/live",
      apiUrl: "https://scan.example:9876",
    });
    expect(initialConfig("?live=javascript:alert(1)")).toMatchObject({
      source: "external",
      commands: false,
      wsUrl: "ws://localhost:8765/live",
    });
  });
});
describe("REST boundary", () => {
  it("surfaces unsupported backend actions without claiming success", async () => {
    const { createServer } = await import("node:http");
    const server = createServer((_req, res) => {
      res.writeHead(501, { "content-type": "application/json" });
      res.end(JSON.stringify({ detail: "Navigation is not implemented" }));
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", resolve),
    );
    const address = server.address() as { port: number };
    try {
      await expect(
        sendCommand(`http://127.0.0.1:${address.port}`, "/goal", {
          x: 1,
          z: 2,
        }),
      ).rejects.toThrow("Navigation is not implemented");
    } finally {
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });
  it("disallows unsafe or mismatched address schemes", () => {
    expect(
      validateConfig({
        source: "external",
        wsUrl: "https://example.com",
        apiUrl: "http://localhost",
        commands: true,
      }),
    ).toContain("ws://");
    expect(
      validateConfig({
        source: "external",
        wsUrl: "ws://localhost/live",
        apiUrl: "javascript:alert(1)",
        commands: true,
      }),
    ).toContain("http://");
  });
});

describe("production feed", () => {
  it("starts on the real backend with commands disabled", () => {
    expect(defaultConfig).toEqual({
      source: "external",
      wsUrl: "ws://localhost:8765/live",
      apiUrl: "http://localhost:8765",
      commands: false,
    });
  });
});
