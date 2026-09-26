const record = (v: unknown): Record<string, any> =>
  v && typeof v === "object" && !Array.isArray(v) ? v : {};

// Visualization can use delayed, calibrated observations. Drive freshness is separate.
export const SURFACE_MAX_AGE_MS = 15_000;

async function boundedBody(response: Response, signal: AbortSignal) {
  const maximum = 32 * 1024 * 1024;
  if (
    !response.ok ||
    Number(response.headers.get("Content-Length")) > maximum
  ) {
    await response.body?.cancel();
    throw Error("Capture unavailable");
  }
  const reader = response.body?.getReader();
  if (!reader) throw Error("Capture empty");
  const chunks: Uint8Array[] = [];
  let count = 0;
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) break;
      count += value.length;
      if (count > maximum) throw Error("Capture too large");
      chunks.push(value);
    }
    signal.throwIfAborted();
  } finally {
    await reader.cancel().catch(() => {});
  }
  const bytes = new Uint8Array(count);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.length;
  }
  return bytes.buffer;
}

/** One compact conditional request on new servers; compatible with older main. */
export class SurfaceFeed {
  private compact = true;
  private etag = "";
  private token = "";
  constructor(
    private base: string,
    private request: typeof fetch = (input, init) => fetch(input, init),
  ) {}

  async read(signal: AbortSignal) {
    const started = performance.now();
    if (this.compact) {
      const response = await this.request(`${this.base}/capture/surface.bin`, {
        signal,
        cache: "no-store",
        headers: this.etag ? { "If-None-Match": this.etag } : {},
      });
      if (response.status === 404 || response.status === 405) {
        await response.body?.cancel();
        this.compact = false;
      } else {
        if (response.status === 204 || response.status === 304) return null;
        const age = Number(response.headers.get("X-Capture-Age-Ms") ?? NaN);
        const expiresAt = started + SURFACE_MAX_AGE_MS - age;
        if (
          response.ok &&
          (!Number.isFinite(age) || age < 0 || performance.now() > expiresAt)
        ) {
          await response.body?.cancel();
          return null;
        }
        const buffer = await boundedBody(response, signal);
        if (performance.now() > expiresAt) return null;
        this.etag = response.headers.get("ETag") ?? "";
        return { buffer, expiresAt, trackingLostCapture: -1 };
      }
    }
    const response = await this.request(`${this.base}/capture/status`, {
      signal,
      cache: "no-store",
    });
    if (!response.ok) return null;
    const status = record(await response.json());
    if (record(status.health).phone === "down") return null;
    const rich = record(record(record(status.rich).packets).frame),
      legacy = record(status.frame);
    const richTime = Number(rich.t_capture),
      legacyTime = Number(record(legacy.metadata).t_capture);
    const sections = Array.isArray(rich.sections)
      ? rich.sections.map((s: unknown) => record(s).name)
      : [];
    const useRich =
      ["rgb", "raw_depth", "raw_confidence"].every((n) =>
        sections.includes(n),
      ) &&
      typeof rich.age_ms === "number" &&
      rich.age_ms <= SURFACE_MAX_AGE_MS &&
      record(rich.metadata).tracking === "normal" &&
      (!Number.isFinite(legacyTime) || richTime >= legacyTime - 0.035);
    const frame = useRich ? rich : legacy;
    const token = String(
      useRich ? (frame.token ?? "") : (frame.capture_id ?? ""),
    );
    const age = frame.age_ms;
    if (
      !token ||
      token === this.token ||
      typeof age !== "number" ||
      !Number.isFinite(age) ||
      age < 0 ||
      age > SURFACE_MAX_AGE_MS
    )
      return null;
    const packet = await this.request(
      `${this.base}/capture/${useRich ? "rich/frame" : "frame"}.bin`,
      { signal, cache: "no-store" },
    );
    const buffer = await boundedBody(packet, signal);
    const expiresAt = started + SURFACE_MAX_AGE_MS - age;
    if (performance.now() > expiresAt) return null;
    this.token = token;
    return {
      buffer,
      expiresAt,
      trackingLostCapture: status.tracking_lost_capture ?? -1,
    };
  }
}
