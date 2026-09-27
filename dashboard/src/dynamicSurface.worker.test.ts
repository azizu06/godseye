import { afterEach, expect, it, vi } from "vitest";
import { capture } from "../tests/captureFixture";
import { SurfaceBuffer, type SurfaceDelta } from "./surfaceBuffer";
import { retainSurface } from "./surfaceStore";
import type { CapturedSurface } from "./surfaceTypes";
afterEach(() => vi.unstubAllGlobals());
it.each([false, true])(
  "fresh background retires moving foreground, dropped near-view preview=%s",
  async (dropPreview) => {
    vi.resetModules();
    const output = new SurfaceBuffer();
    let latest: ReturnType<SurfaceBuffer["apply"]> = null;
    let displayed: CapturedSurface[] = [];
    let ignorePreview = false;
    const replies: {
      preview?: { surface: CapturedSurface };
      delta?: SurfaceDelta;
      error?: string;
      retiredSurfaces?: { id: string; indices: Uint32Array }[];
    }[] = [];
    const worker = {
      onmessage: null as ((event: MessageEvent) => Promise<void>) | null,
      postMessage: (value: (typeof replies)[number]) => {
        replies.push(value);
        if (value.preview && !ignorePreview)
          displayed = retainSurface(
            displayed,
            value.preview.surface,
            JSON.stringify(["surface-room", 1]),
          );
        for (const update of value.retiredSurfaces ?? [])
          displayed = displayed
            .map((patch) =>
              patch.id === update.id
                ? { ...patch, indices: update.indices }
                : patch,
            )
            .filter((patch) => patch.indices.length);
        if (value.delta) latest = output.apply(value.delta);
      },
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
    const send = async (frame: number, person: boolean, expire = false) => {
      ignorePreview = dropPreview && frame === 3;
      const packet = capture(1, 2, frame, person || ignorePreview ? 0.3 : 0);
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
      let calls = 0;
      const clock = expire
        ? vi
            .spyOn(Date, "now")
            .mockImplementation(() => (++calls >= 5 ? 100000 : 0))
        : null;
      await worker.onmessage!(
        new MessageEvent("message", {
          data: {
            id: frame,
            retainedSurfaceIds: displayed.map((patch) => patch.id),
            buffer: packet.buffer.slice(
              packet.byteOffset,
              packet.byteOffset + packet.byteLength,
            ),
            expectedMap: JSON.stringify(["surface-room", 1]),
            expiresAt: Date.now() + 30000,
          },
        }),
      );
      clock?.mockRestore();
      if (expire)
        expect(replies.at(-1)?.error).toContain("expired before integration");
      else expect(replies.at(-1)?.error).toBeUndefined();
    };
    const foreground = () => {
      const p = latest;
      return p
        ? [...new Set(p.indices)].filter((i) => p.positions[i * 3 + 2] > -1.3)
            .length
        : 0;
    };
    await send(1, false);
    await send(2, true);
    expect(foreground()).toBeGreaterThan(0);
    await send(3, false);
    expect(foreground()).toBeGreaterThan(0); // One newer depth image is insufficient.
    await send(3, false); // Polling repeats cannot become a second observation.
    expect(foreground()).toBeGreaterThan(0);
    if (!dropPreview) await send(4, false, true); // A late failure must roll back topology and keep texture removal retryable.
    expect(foreground()).toBeGreaterThan(0);
    await send(4, false); // Repeated background is already known to the keyframe cache.
    if (dropPreview) await send(5, false);
    expect(foreground()).toBe(0);
    expect(latest!.indices.length).toBeGreaterThan(0);
    expect(
      displayed.flatMap((patch) =>
        [...patch.indices].filter((i) => patch.positions[i * 3 + 2] > -1.3),
      ),
    ).toHaveLength(0);
    expect(
      replies.some((reply) =>
        reply.retiredSurfaces?.some(
          (p) => p.id === JSON.stringify(["surface-room", 1, 2, 1]),
        ),
      ),
    ).toBe(true);
  },
);
