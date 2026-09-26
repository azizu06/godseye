import { afterEach, expect, it, vi } from "vitest";
import type { SurfacePatch } from "./surfaceTypes";
afterEach(() => vi.unstubAllGlobals());
it("returns retained snapshots on redundant views and only remembers successful integration", async () => {
  const replies: { patch?: SurfacePatch; error?: string }[] = [];
  const worker = {
    onmessage: null as ((event: MessageEvent) => void) | null,
    postMessage: (value: { patch?: SurfacePatch; error?: string }) =>
      replies.push(value),
  };
  vi.stubGlobal("self", worker);
  await import("./surfaceMap.worker");
  const patch = (x: number, z: number): SurfacePatch => ({
    id: `${x}:${z}`,
    positions: new Float32Array([x, 0, z, x + 1, 0, z, x, 1, z]),
    indices: new Uint32Array([0, 1, 2]),
    colors: new Float32Array([1, 0, 0, 1, 0, 0, 1, 0, 0]),
  });
  const send = (value: SurfacePatch, cameraX = 0) =>
    worker.onmessage!(
      new MessageEvent("message", {
        data: {
          id: replies.length,
          patch: value,
          viewpoint: {
            sessionId: "room",
            mapEpoch: 1,
            cameraPosition: [cameraX, 0, 0],
            cameraForward: [0, 0, -1],
          },
        },
      }),
    );
  send(patch(0, 0));
  const first = replies.at(-1)!.patch!;
  send(patch(0.01, 0));
  expect(replies.at(-1)!.patch).toBe(first);
  send(patch(0, 0.3));
  expect(replies.at(-1)!.patch!.positions.length).toBe(18);
  const failed = patch(2, 0);
  failed.colors![0] = NaN;
  send(failed, 0.3);
  expect(replies.at(-1)!.error).toMatch(/colors/);
  send(patch(2, 0), 0.3);
  expect(replies.at(-1)!.patch!.positions.length).toBe(27);
});
