import { describe, expect, it } from "vitest";
import { decodeCaptureSurface } from "./captureSurface";

const JPEG = Uint8Array.from(
  Buffer.from(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDl6KKKZ92f/9k=",
    "base64",
  ),
);
// Camera rotated 90 degrees around Y, translated by (1,2,3).
const TRANSFORM = [0, 0, -1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 2, 3, 1];
const K = [16, 0, 0, 0, 16, 0, 4, 4, 1];
function fixture(
  version = 1,
  edit: (header: any) => void = () => {},
  depths = Array(16).fill(2),
  confidence = Array(16).fill(2),
) {
  const pose = {
    session_id: "test-map",
    map_epoch: 2,
    frame_id: 7,
    t_capture: 1.5,
    t_wall_ms: 1000,
    transform: TRANSFORM,
    tracking: "normal",
  };
  const header: any =
    version === 1
      ? {
          version: 1,
          type: "frame",
          ...pose,
          image: {
            width: 8,
            height: 8,
            jpeg_len: JPEG.length,
            intrinsics: K,
            orientation: "landscape_right",
          },
          depth: { width: 4, height: 4, len: 64, format: "float32_m" },
          confidence: { width: 4, height: 4, len: 16, format: "uint8_0_2" },
        }
      : {
          version: 2,
          type: "capture",
          kind: "frame",
          ...pose,
          metadata: {
            ...pose,
            native_image: {
              width: 8,
              height: 8,
              intrinsics: K,
              orientation: "landscape_right",
            },
          },
          sections: [
            {
              name: "rgb",
              format: "jpeg",
              offset: 0,
              length: JPEG.length,
              shape: [8, 8],
            },
            {
              name: "raw_depth",
              format: "f32le",
              offset: JPEG.length,
              length: 64,
              shape: [4, 4],
            },
            {
              name: "raw_confidence",
              format: "u8",
              offset: JPEG.length + 64,
              length: 16,
              shape: [4, 4],
            },
          ],
        };
  edit(header);
  let json = JSON.stringify(header);
  // Deliberately leave float data unaligned, as real variable-length JPEGs do.
  while ((4 + new TextEncoder().encode(json).length + JPEG.length) % 4 === 0)
    json += " ";
  const encoded = new TextEncoder().encode(json);
  const data = new Uint8Array(
    4 + encoded.length + JPEG.length + depths.length * 4 + confidence.length,
  );
  const view = new DataView(data.buffer);
  view.setUint32(0, encoded.length, true);
  data.set(encoded, 4);
  const start = 4 + encoded.length;
  data.set(JPEG, start);
  depths.forEach((value, i) =>
    view.setFloat32(start + JPEG.length + i * 4, value, true),
  );
  data.set(confidence, start + JPEG.length + depths.length * 4);
  return data.buffer;
}

describe("calibrated capture surfaces", () => {
  for (const version of [1, 2]) {
    it(`projects v${version} same-frame RGB-D with column-major transform and top-left image UVs`, () => {
      const patch = decodeCaptureSurface(fixture(version));
      // Pixel center (1,1), depth 2 gives camera (-.375,.375,-2), world(-1,2.375,3.375).
      expect(Array.from(patch.positions.slice(0, 3))).toEqual([
        -1, 2.375, 3.375,
      ]);
      expect(Array.from(patch.uvs!.slice(0, 2))).toEqual([0.125, 0.875]);
      expect(Array.from(patch.indices.slice(0, 3))).toEqual([0, 4, 1]);
      expect(patch.indices.length).toBe(54);
      expect(patch.cameraPosition).toEqual([1, 2, 3]);
      expect(patch.cameraForward).toEqual([-1, 0, 0]);
      expect([
        patch.sessionId,
        patch.mapEpoch,
        patch.frameId,
        patch.capturedAt,
      ]).toEqual(["test-map", 2, 7, 1.5]);
      expect(patch.jpeg).toEqual(JPEG);
    });
  }
  it("omits invalid, low-confidence, and depth-discontinuity triangles", () => {
    const depth = Array(16).fill(2);
    depth[0] = NaN;
    depth[1] = Infinity;
    depth[2] = 0;
    depth[3] = 5.1;
    depth[5] = 3;
    const confidence = Array(16).fill(2);
    confidence[4] = 0;
    const patch = decodeCaptureSurface(fixture(1, () => {}, depth, confidence));
    expect(patch.indices.length).toBeLessThan(54);
    expect(patch.indices.length).toBeGreaterThan(0);
    for (const i of patch.indices) expect(patch.positions[i * 3]).toBe(-1);
    expect([...patch.positions].every(Number.isFinite)).toBe(true);
  });
  it("does not invent geometry when confidence is unavailable", () => {
    const patch = decodeCaptureSurface(
      fixture(2, () => {}, Array(16).fill(2), Array(16).fill(0)),
    );
    expect(patch.indices.length).toBe(0);
    expect(patch.positions.length).toBe(0);
  });
  it("rejects malformed lengths, calibration, tracking, identities and JPEG dimensions", () => {
    for (const edit of [
      (h: any) => h.depth.len++,
      (h: any) => h.confidence.width++,
      (h: any) => h.image.width++,
      (h: any) => (h.image.intrinsics = [0, 0, 0, 0, 16, 0, 4, 4, 1]),
      (h: any) => (h.transform = TRANSFORM.map((v, i) => (i === 0 ? 1 : v))),
      (h: any) =>
        (h.transform = TRANSFORM.map((v, i) => (i === 12 ? null : v))),
      (h: any) => (h.tracking = "limited"),
      (h: any) => (h.map_epoch = -1),
      (h: any) => (h.image.orientation = "portrait"),
    ])
      expect(() => decodeCaptureSurface(fixture(1, edit))).toThrow();
    expect(() => decodeCaptureSurface(fixture().slice(0, -1))).toThrow();
    expect(() =>
      decodeCaptureSurface(new ArrayBuffer(33 * 1024 * 1024)),
    ).toThrow();
  });
  it("validates every v2 section and requires matching raw confidence", () => {
    for (const edit of [
      (h: any) => h.sections[1].offset++,
      (h: any) => (h.sections[2].name = "smoothed_confidence"),
      (h: any) => (h.sections[2].shape = [2, 8]),
      (h: any) => (h.sections[2].name = "rgb"),
      (h: any) => (h.kind = "geometry"),
      (h: any) => (h.metadata.session_id = "other-map"),
    ])
      expect(() => decodeCaptureSurface(fixture(2, edit))).toThrow();
  });
  it("bounds dense input and does not bridge unsampled invalid depth strips", () => {
    const confidence = Array(256 * 256).fill(2);
    for (let row = 0; row < 256; row++) confidence[row * 256 + 1] = 0;
    const patch = decodeCaptureSurface(
      fixture(
        1,
        (h) => {
          h.depth = { ...h.depth, width: 256, height: 256, len: 256 * 256 * 4 };
          h.confidence = {
            ...h.confidence,
            width: 256,
            height: 256,
            len: 256 * 256,
          };
        },
        Array(256 * 256).fill(2),
        confidence,
      ),
    );
    expect(patch.positions.length / 3).toBeLessThanOrEqual(12_288);
    expect(patch.indices.length).toBeGreaterThan(1000);
    expect([...patch.indices]).not.toContain(0);
  });
  it("covers measured medium-confidence walls with the coarse preview", () => {
    const patch = decodeCaptureSurface(
      fixture(1, () => {}, Array(16).fill(2), Array(16).fill(1)),
    );
    expect(patch.positions.length / 3).toBe(16);
    expect(patch.indices.length / 3).toBe(18);
  });

  it("uses larger live triangles without clipping the last measured image row or column", () => {
    const patch = decodeCaptureSurface(
      fixture(
        1,
        (h) => {
          h.depth = { ...h.depth, width: 256, height: 256, len: 256 * 256 * 4 };
          h.confidence = {
            ...h.confidence,
            width: 256,
            height: 256,
            len: 256 * 256,
          };
        },
        Array(256 * 256).fill(2),
        Array(256 * 256).fill(1),
      ),
    );
    expect(patch.positions.length / 3).toBeGreaterThan(2000);
    expect(patch.positions.length / 3).toBeLessThanOrEqual(3072);
    const uv = patch.uvs!;
    expect(uv[0]).toBeCloseTo(0.5 / 256);
    expect(uv[1]).toBeCloseTo(1 - 0.5 / 256);
    expect(uv[uv.length - 2]).toBeCloseTo(255.5 / 256);
    expect(uv[uv.length - 1]).toBeCloseTo(1 - 255.5 / 256);
    expect(patch.indices.length / 3).toBeGreaterThan(4000);
  });

  it("retains a continuous steep wall rather than mistaking coarse-cell slope for a depth break", () => {
    const depths = Array.from({ length: 256 * 256 }, (_, index) => {
      const u = (((index % 256) + 0.5) * 8) / 256;
      return 2 / (1 - ((u - 4) / 16) * Math.tan(Math.PI / 3));
    });
    const patch = decodeCaptureSurface(
      fixture(
        1,
        (h) => {
          h.depth = { ...h.depth, width: 256, height: 256, len: 256 * 256 * 4 };
          h.confidence = {
            ...h.confidence,
            width: 256,
            height: 256,
            len: 256 * 256,
          };
        },
        depths,
        Array(256 * 256).fill(2),
      ),
    );
    const side = Math.sqrt(patch.positions.length / 3);
    expect(patch.indices.length / 6).toBeGreaterThan((side - 1) ** 2 * 0.99);
  });

  it.each(["missing", "nan", "silhouette", "invalid-confidence"])(
    "does not bridge skipped native %s pixels inside a coarse cell",
    (kind) => {
      const depths = Array(256 * 256).fill(2),
        confidence = Array(256 * 256).fill(1);
      for (let row = 0; row < 256; row++) {
        const index = row * 256 + 2;
        if (kind === "nan") depths[index] = NaN;
        else if (kind === "silhouette") depths[index] = 2.5;
        else confidence[index] = kind === "missing" ? 0 : 255;
      }
      const patch = decodeCaptureSurface(
        fixture(
          1,
          (h) => {
            h.depth = {
              ...h.depth,
              width: 256,
              height: 256,
              len: 256 * 256 * 4,
            };
            h.confidence = {
              ...h.confidence,
              width: 256,
              height: 256,
              len: 256 * 256,
            };
          },
          depths,
          confidence,
        ),
      );
      expect(patch.indices.length).toBeGreaterThan(1000);
      expect([...patch.indices]).not.toContain(0);
    },
  );

  it.each([3, 255])(
    "rejects malformed confidence %s at retained samples",
    (confidence) => {
      const patch = decodeCaptureSurface(
        fixture(1, () => {}, Array(16).fill(2), Array(16).fill(confidence)),
      );
      expect(patch.positions.length).toBe(0);
      expect(patch.indices.length).toBe(0);
    },
  );
});
