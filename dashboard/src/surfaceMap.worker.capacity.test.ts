import { afterEach, expect, it, vi } from "vitest";
import { capture } from "../tests/captureFixture";

// The map is replaced so one integration can report bounded-work capacity.
const state = vi.hoisted(() => ({ adds: 0, saturated: false }));
vi.mock("./persistentSurfaceMap", () => ({
  PersistentSurfaceMap: class {
    cellM = 0;
    get atCapacity() {
      return state.saturated;
    }
    commitRetirement() {}
    retirementCheckpoint() {
      return () => {};
    }
    retire() {
      return 0;
    }
    add() {
      if (++state.adds === 1)
        throw Error(
          "Surface map capacity reached: coarsening exceeded its bounded processing time.",
        );
      return { retained: null, reset: true };
    }
    takeDelta() {
      return {
        revision: state.adds,
        reset: false,
        vertexStart: 0,
        indexStart: 0,
        vertexCount: 0,
        indexCount: 0,
        positions: new Float32Array(),
        colors: new Float32Array(),
        indices: new Uint32Array(),
      };
    }
  },
}));
afterEach(() => vi.unstubAllGlobals());

it("keeps integrating later views after one capacity failure and reports saturation", async () => {
  const replies: { capacity?: boolean; delta?: unknown; error?: string }[] = [];
  const worker = {
    onmessage: null as ((event: MessageEvent) => Promise<void>) | null,
    postMessage: (value: (typeof replies)[number]) => replies.push(value),
  };
  vi.stubGlobal("self", worker);
  vi.stubGlobal("createImageBitmap", async () => ({
    width: 80,
    height: 60,
    close() {},
  }));
  vi.stubGlobal(
    "OffscreenCanvas",
    class {
      constructor(
        public width: number,
        public height: number,
      ) {}
      getContext() {
        return {
          drawImage() {},
          clearRect() {},
          translate() {},
          scale() {},
          getImageData: (_x: number, _y: number, w: number, h: number) => ({
            width: w,
            height: h,
            data: new Uint8ClampedArray(w * h * 4).fill(128),
          }),
        };
      }
      transferToImageBitmap() {
        return { close() {} };
      }
    },
  );
  await import("./surfaceMap.worker");
  const send = async (frame: number, x: number) => {
    const packet = capture(1, 2, frame, x);
    await worker.onmessage!(
      new MessageEvent("message", {
        data: {
          id: frame,
          buffer: packet.buffer.slice(
            packet.byteOffset,
            packet.byteOffset + packet.byteLength,
          ),
          expectedMap: JSON.stringify(["surface-room", 1]),
          expiresAt: Date.now() + 60_000,
        },
      }),
    );
    return replies.filter((reply) => reply.delta).at(-1)!;
  };
  expect((await send(7, 0)).capacity).toBe(true);
  // A view from elsewhere is still offered to the map and clears the status.
  expect((await send(8, 2)).capacity).toBe(false);
  expect(state.adds).toBe(2);
  // Once the map reports no coarsening room, capacity stays truthful.
  state.saturated = true;
  expect((await send(9, 4)).capacity).toBe(true);
  expect(state.adds).toBe(3);
  expect(replies.some((reply) => reply.error)).toBe(false);
});
