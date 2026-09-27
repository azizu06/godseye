import { afterEach, expect, it, vi } from "vitest";
import { capture } from "../tests/captureFixture";
import type { PersonClearance, PersonProbe } from "./personMemory";

afterEach(() => vi.unstubAllGlobals());

// Actual surface worker and depth proof; only browser image APIs are stubbed.
async function surfaceWorker() {
  vi.resetModules();
  const replies: {
    error?: string;
    preview?: unknown;
    clearedPeople?: PersonClearance[];
  }[] = [];
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
  // Camera at (cameraX, 1, 0) looking down -Z at a 2 m wall; a person stands at 1 m.
  return async (
    frame: number,
    people: PersonProbe[],
    { person = false, cameraX = 0 } = {},
  ) => {
    const packet = capture(1, 2, frame, cameraX);
    const headerLength = packet.readUInt32LE(0);
    const header = JSON.parse(packet.toString("utf8", 4, 4 + headerLength));
    const offset =
      4 +
      headerLength +
      header.sections.find((s: { name: string }) => s.name === "raw_depth")
        .offset;
    if (person)
      for (let y = 4; y < 11; y++)
        for (let x = 6; x < 14; x++)
          packet.writeFloatLE(1, offset + (y * 20 + x) * 4);
    await worker.onmessage!(
      new MessageEvent("message", {
        data: {
          id: frame,
          buffer: packet.buffer.slice(
            packet.byteOffset,
            packet.byteOffset + packet.byteLength,
          ),
          expectedMap: JSON.stringify(["surface-room", 1]),
          expiresAt: Date.now() + 30000,
          people,
        },
      }),
    );
    const reply = replies.at(-1)!;
    expect(reply.error).toBeUndefined();
    return reply.clearedPeople ?? [];
  };
}

// Measured by the detector in frame 2 (t_capture 1 s), then no longer detected.
const person: PersonProbe = {
  id: "person-1",
  measuredAt: 1,
  position: [0, 1, -1],
};

it("keeps a remembered person while the camera looks away and clears it after two newer views of the empty spot", async () => {
  const send = await surfaceWorker();
  expect(await send(1, [])).toEqual([]);
  expect(await send(2, [person], { person: true })).toEqual([]);
  // Looking away: the remembered extent is outside both newer views.
  expect(await send(3, [person], { cameraX: 10 })).toEqual([]);
  expect(await send(4, [person], { cameraX: 10 })).toEqual([]);
  // Camera returns and the person is still there (missed by the detector).
  expect(await send(5, [person], { person: true })).toEqual([]);
  // One newer empty view is not enough; a repeated capture is not a second view.
  expect(await send(6, [person])).toEqual([]);
  expect(await send(6, [person])).toEqual([]);
  expect(await send(7, [person])).toEqual([{ id: "person-1", measuredAt: 1 }]);
});

it("never clears on proof views older than the latest measurement", async () => {
  const send = await surfaceWorker();
  await send(1, []);
  await send(2, []);
  // Re-measured at frame 3 (t_capture 1.5 s): views 2 and 3 predate it.
  expect(await send(3, [{ ...person, measuredAt: 1.5 }])).toEqual([]);
  expect(await send(4, [{ ...person, measuredAt: 1.5 }])).toEqual([]);
  expect(await send(5, [{ ...person, measuredAt: 1.5 }])).toEqual([
    { id: "person-1", measuredAt: 1.5 },
  ]);
});
