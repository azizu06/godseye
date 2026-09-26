import type { CapturedSurface } from "./surfaceTypes";

type RecordValue = Record<string, unknown>;
const MAX_PACKET = 32 * 1024 * 1024;
// Coarse display geometry: native 256×192 depth uses about stride four.
const MAX_VERTICES = 3_072;
const supportedConfidence = (value: number) => value === 1 || value === 2;
function fail(message: string): never {
  throw Error(`Invalid surface capture: ${message}`);
}
function record(value: unknown): RecordValue {
  if (!value || typeof value !== "object" || Array.isArray(value))
    fail("expected metadata object");
  return value as RecordValue;
}
function integer(
  value: unknown,
  min = 0,
  max = Number.MAX_SAFE_INTEGER,
): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < min ||
    value > max
  )
    fail("invalid integer");
  return value;
}
function finite(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    fail("nonfinite number");
  return value;
}
function matrix(value: unknown, length: number): number[] {
  if (!Array.isArray(value) || value.length !== length)
    fail("invalid matrix size");
  return value.map(finite);
}
function dimensions(
  width: unknown,
  height: unknown,
  limit: number,
): [number, number] {
  const w = integer(width, 1, 8192),
    h = integer(height, 1, 8192);
  if (w * h > limit) fail("image dimensions exceed limit");
  return [w, h];
}
function validateTransform(t: number[]) {
  if ([t[3], t[7], t[11], t[15] - 1].some((v) => Math.abs(v) > 0.0001))
    fail("non-affine transform");
  for (let a = 0; a < 3; a++)
    for (let b = 0; b < 3; b++) {
      const dot =
        t[a * 4] * t[b * 4] +
        t[a * 4 + 1] * t[b * 4 + 1] +
        t[a * 4 + 2] * t[b * 4 + 2];
      if (Math.abs(dot - (a === b ? 1 : 0)) > 0.002)
        fail("transform is not rigid");
    }
  const det =
    t[0] * (t[5] * t[10] - t[9] * t[6]) -
    t[4] * (t[1] * t[10] - t[9] * t[2]) +
    t[8] * (t[1] * t[6] - t[5] * t[2]);
  if (Math.abs(det - 1) > 0.002) fail("transform reverses handedness");
}
function jpegDimensions(jpeg: Uint8Array): [number, number] {
  if (
    jpeg.length < 4 ||
    jpeg[0] !== 255 ||
    jpeg[1] !== 216 ||
    jpeg.at(-2) !== 255 ||
    jpeg.at(-1) !== 217
  )
    fail("missing JPEG markers");
  let p = 2;
  while (p + 3 < jpeg.length) {
    if (jpeg[p++] !== 255) fail("invalid JPEG marker");
    while (jpeg[p] === 255) p++;
    const marker = jpeg[p++];
    if (marker === 218 || marker === 217) break;
    if (marker === 1 || (marker >= 208 && marker <= 215)) continue;
    const size = (jpeg[p] << 8) | jpeg[p + 1];
    if (size < 2 || p + size > jpeg.length) fail("invalid JPEG segment");
    if ([192, 193, 194].includes(marker)) {
      if (size < 8) fail("invalid JPEG dimensions");
      return [
        (jpeg[p + 5] << 8) | jpeg[p + 6],
        (jpeg[p + 3] << 8) | jpeg[p + 4],
      ];
    }
    p += size;
  }
  return fail("unsupported JPEG frame");
}

/** Decode same-frame calibrated RGB-D; UVs assume a conventional flipY=true JPEG texture. */
export function decodeCaptureSurface(buffer: ArrayBuffer): CapturedSurface {
  if (buffer.byteLength < 5 || buffer.byteLength > MAX_PACKET)
    fail("packet size exceeds bounds");
  const view = new DataView(buffer);
  const headerLength = view.getUint32(0, true);
  if (
    !headerLength ||
    headerLength > 2 * 1024 * 1024 ||
    headerLength + 4 > buffer.byteLength
  )
    fail("invalid header length");
  const header = record(
    JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(
        new Uint8Array(buffer, 4, headerLength),
      ),
    ),
  );
  const start = 4 + headerLength;
  let image: RecordValue,
    pose: RecordValue,
    dw: number,
    dh: number,
    jpegOffset: number,
    jpegLength: number,
    depthOffset: number,
    confidenceOffset: number;
  if (header.version === 1 && header.type === "frame") {
    if (headerLength > 65536) fail("v1 header exceeds bounds");
    pose = header;
    image = record(header.image);
    const depth = record(header.depth),
      confidence = record(header.confidence);
    [dw, dh] = dimensions(depth.width, depth.height, 1_048_576);
    if (
      depth.format !== "float32_m" ||
      confidence.format !== "uint8_0_2" ||
      confidence.width !== dw ||
      confidence.height !== dh
    )
      fail("depth/confidence dimensions or format mismatch");
    if (depth.len !== dw * dh * 4 || confidence.len !== dw * dh)
      fail("depth/confidence byte mismatch");
    jpegLength = integer(image.jpeg_len, 1, MAX_PACKET);
    jpegOffset = start;
    depthOffset = start + jpegLength;
    confidenceOffset = depthOffset + dw * dh * 4;
    if (confidenceOffset + dw * dh !== buffer.byteLength)
      fail("v1 body length mismatch");
  } else if (
    header.version === 2 &&
    header.type === "capture" &&
    header.kind === "frame"
  ) {
    pose = record(header.metadata);
    image = record(pose.native_image);
    for (const key of ["session_id", "map_epoch", "frame_id", "t_capture"])
      if (pose[key] !== undefined && pose[key] !== header[key])
        fail("frame metadata identity mismatch");
    if (!Array.isArray(header.sections) || header.sections.length > 8192)
      fail("invalid section collection");
    const sections = new Map<string, RecordValue>();
    const itemBytes: Record<string, number> = {
      u8: 1,
      u16le: 2,
      u32le: 4,
      u64le: 8,
      f32le: 4,
    };
    let offset = 0;
    for (const raw of header.sections) {
      const section = record(raw),
        name = section.name,
        format = section.format;
      if (
        typeof name !== "string" ||
        !/^[a-zA-Z0-9_.-]{1,100}$/.test(name) ||
        sections.has(name)
      )
        fail("invalid or repeated section name");
      if (section.offset !== offset) fail("noncontiguous sections");
      const length = integer(section.length, 0, MAX_PACKET);
      if (
        !Array.isArray(section.shape) ||
        !section.shape.length ||
        section.shape.length > 3
      )
        fail("invalid section shape");
      const shape = section.shape.map((n) => integer(n, 0, MAX_PACKET));
      const count = shape.reduce((a, b) => a * b, 1);
      if (!Number.isSafeInteger(count)) fail("section shape exceeds bounds");
      if (format === "jpeg") {
        if (shape.length !== 2 || count <= 0 || count > 64_000_000 || !length)
          fail("invalid JPEG section");
      } else if (
        typeof format !== "string" ||
        !Object.hasOwn(itemBytes, format) ||
        count * itemBytes[format] !== length
      )
        fail("section format/length mismatch");
      offset += length;
      if (start + offset > buffer.byteLength) fail("section exceeds packet");
      sections.set(name, section);
    }
    if (start + offset !== buffer.byteLength) fail("v2 body length mismatch");
    const rgb = sections.get("rgb"),
      depth = sections.get("raw_depth"),
      confidence = sections.get("raw_confidence");
    if (
      !rgb ||
      !depth ||
      !confidence ||
      rgb.format !== "jpeg" ||
      depth.format !== "f32le" ||
      confidence.format !== "u8"
    )
      fail("RGB/raw depth/raw confidence unavailable");
    const ds = depth.shape as number[],
      cs = confidence.shape as number[],
      rs = rgb.shape as number[];
    if (
      ds.length !== 2 ||
      cs.length !== 2 ||
      cs[0] !== ds[0] ||
      cs[1] !== ds[1]
    )
      fail("depth/confidence shape mismatch");
    [dw, dh] = dimensions(ds[1], ds[0], 1_048_576);
    if (rs[1] !== image.width || rs[0] !== image.height)
      fail("RGB metadata dimensions mismatch");
    jpegOffset = start + Number(rgb.offset);
    jpegLength = Number(rgb.length);
    depthOffset = start + Number(depth.offset);
    confidenceOffset = start + Number(confidence.offset);
  } else return fail("unsupported packet version/type");
  const sessionId = header.session_id;
  if (
    typeof sessionId !== "string" ||
    !sessionId.length ||
    sessionId.length > 256
  )
    fail("invalid session identity");
  const mapEpoch = integer(header.map_epoch, 1),
    frameId = integer(header.frame_id),
    capturedAt = finite(header.t_capture);
  integer(header.t_wall_ms);
  if (capturedAt < 0 || pose.tracking !== "normal")
    fail("capture tracking unavailable");
  const transform = matrix(pose.transform, 16);
  validateTransform(transform);
  const [iw, ih] = dimensions(image.width, image.height, 16_000_000);
  if (iw * dh !== ih * dw || image.orientation !== "landscape_right")
    fail("unaligned image/depth orientation");
  const k = matrix(image.intrinsics, 9);
  if (
    k[0] <= 0 ||
    k[4] <= 0 ||
    k[6] < 0 ||
    k[6] >= iw ||
    k[7] < 0 ||
    k[7] >= ih ||
    k[8] !== 1 ||
    [k[1], k[2], k[3], k[5]].some((n) => n !== 0)
  )
    fail("invalid camera intrinsics");
  const jpeg = new Uint8Array(buffer, jpegOffset, jpegLength).slice();
  const [jw, jh] = jpegDimensions(jpeg);
  if (jw !== iw || jh !== ih) fail("JPEG dimensions disagree with calibration");
  let stride = Math.max(1, Math.ceil(Math.sqrt((dw * dh) / MAX_VERTICES)));
  while (Math.ceil(dw / stride) * Math.ceil(dh / stride) > MAX_VERTICES)
    stride++;
  const cols = Math.ceil(dw / stride),
    rows = Math.ceil(dh / stride);
  // Spread samples across the full image so coarsening does not clip its edges.
  const sampleColumns = Array.from({ length: cols }, (_, i) =>
    cols === 1 ? 0 : Math.round((i * (dw - 1)) / (cols - 1)),
  );
  const sampleRows = Array.from({ length: rows }, (_, i) =>
    rows === 1 ? 0 : Math.round((i * (dh - 1)) / (rows - 1)),
  );
  const sampleSpan = Math.max(
    cols === 1 ? 1 : Math.ceil((dw - 1) / (cols - 1)),
    rows === 1 ? 1 : Math.ceil((dh - 1) / (rows - 1)),
  );
  const lookup = new Int32Array(cols * rows).fill(-1);
  const positions: number[] = [],
    uvs: number[] = [],
    depths: number[] = [],
    indices: number[] = [];
  for (let row = 0; row < rows; row++)
    for (let col = 0; col < cols; col++) {
      const r = sampleRows[row],
        c = sampleColumns[col],
        pixel = r * dw + c;
      const depth = view.getFloat32(depthOffset + pixel * 4, true);
      if (
        !supportedConfidence(view.getUint8(confidenceOffset + pixel)) ||
        !Number.isFinite(depth) ||
        depth < 0.05 ||
        depth > 5
      )
        continue;
      const u = ((c + 0.5) * iw) / dw,
        v = ((r + 0.5) * ih) / dh;
      const x = ((u - k[6]) * depth) / k[0],
        y = (-(v - k[7]) * depth) / k[4],
        z = -depth;
      const world = [0, 1, 2].map(
        (a) =>
          transform[a] * x +
          transform[4 + a] * y +
          transform[8 + a] * z +
          transform[12 + a],
      );
      if (
        !world.every(
          (n) => Number.isFinite(n) && Number.isFinite(Math.fround(n)),
        )
      )
        fail("world coordinates exceed renderer bounds");
      lookup[row * cols + col] = depths.length;
      depths.push(depth);
      positions.push(...world);
      uvs.push(u / iw, 1 - v / ih);
    }
  const triangle = (a: number, b: number, c: number) => {
    if (a < 0 || b < 0 || c < 0) return;
    const low = Math.min(depths[a], depths[b], depths[c]),
      high = Math.max(depths[a], depths[b], depths[c]);
    // Do not bridge silhouettes. These are conservative rendering tolerances,
    // not sensor accuracy claims or inferred surfaces across missing readings.
    // Coarse cells have already checked every native neighbor for a depth
    // break. Their full span can legitimately be larger on a slanted wall.
    if (stride === 1 && high - low > Math.max(0.05, 0.03 * low)) return;
    const maxEdge = Math.max(
      0.1,
      3 * high * sampleSpan * Math.max(iw / dw / k[0], ih / dh / k[4]) +
        // Native neighbor checks already establish continuous depth support.
        // Account for its measured range span when bounding a slanted edge.
        (stride > 1
          ? (high - low) *
            Math.hypot(
              1,
              Math.max(k[6], iw - k[6]) / k[0],
              Math.max(k[7], ih - k[7]) / k[4],
            )
          : 0),
    );
    for (const [i, j] of [
      [a, b],
      [b, c],
      [c, a],
    ])
      if (
        Math.hypot(
          positions[i * 3] - positions[j * 3],
          positions[i * 3 + 1] - positions[j * 3 + 1],
          positions[i * 3 + 2] - positions[j * 3 + 2],
        ) > maxEdge
      )
        return;
    indices.push(a, b, c);
  };
  for (let row = 0; row < rows - 1; row++)
    for (let col = 0; col < cols - 1; col++) {
      if (stride > 1) {
        // Subsampling must not connect across an invalid strip hidden between
        // retained vertices. Conservatively require support across the cell.
        let supported = true;
        for (
          let r = sampleRows[row];
          r <= sampleRows[row + 1] && supported;
          r++
        )
          for (let c = sampleColumns[col]; c <= sampleColumns[col + 1]; c++) {
            const pixel = r * dw + c,
              depth = view.getFloat32(depthOffset + pixel * 4, true);
            if (
              !supportedConfidence(view.getUint8(confidenceOffset + pixel)) ||
              !Number.isFinite(depth) ||
              depth < 0.05 ||
              depth > 5
            ) {
              supported = false;
              break;
            }
            // Test native adjacent samples rather than the whole coarse-cell
            // depth span: a continuous slope is not an object silhouette.
            const neighbors = [
              ...(c > sampleColumns[col] ? [pixel - 1] : []),
              ...(r > sampleRows[row] ? [pixel - dw] : []),
            ];
            if (
              neighbors.some((neighbor) => {
                const other = view.getFloat32(depthOffset + neighbor * 4, true);
                return (
                  Math.abs(depth - other) >
                  Math.max(0.05, 0.03 * Math.min(depth, other))
                );
              })
            ) {
              supported = false;
              break;
            }
          }
        if (!supported) continue;
      }
      const a = row * cols + col,
        b = a + cols;
      triangle(lookup[a], lookup[b], lookup[a + 1]);
      triangle(lookup[a + 1], lookup[b], lookup[b + 1]);
    }
  return {
    id: JSON.stringify([sessionId, mapEpoch, frameId, capturedAt]),
    sessionId,
    mapEpoch,
    frameId,
    capturedAt,
    cameraPosition: [transform[12], transform[13], transform[14]],
    cameraForward: [
      -transform[8] || 0,
      -transform[9] || 0,
      -transform[10] || 0,
    ],
    positions: new Float32Array(positions),
    indices: new Uint32Array(indices),
    uvs: new Float32Array(uvs),
    jpeg,
  };
}
