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
  issue = "";
  ageMs = 0;
  private expiresAt = 0;
  private compact = true;
  private etag = "";
  private token = "";
  private pendingEtag = "";
  private pendingToken = "";
  commit() {
    this.etag = this.pendingEtag;
    this.token = this.pendingToken;
  }
  constructor(
    private base: string,
    private request: typeof fetch = (input, init) => fetch(input, init),
  ) {}

  async read(signal: AbortSignal) {
    this.issue = "";
    const started = performance.now();
    if (this.compact) {
      const response = await this.request(`${this.base}/capture/surface.bin`, {
        signal,
        cache: "no-store",
        headers: this.etag ? { "If-None-Match": this.etag } : {},
      });
      const contentType = response.headers.get("Content-Type") ?? "";
      if (
        response.status === 404 ||
        response.status === 405 ||
        (response.status === 200 &&
          /application\/json|text\/html/.test(contentType))
      ) {
        await response.body?.cancel();
        this.compact = false;
      } else {
        if (response.status === 204) {
          this.issue = "No eligible RGB-D capture received; scan retained";
          return null;
        }
        if (response.status === 304) {
          this.ageMs = SURFACE_MAX_AGE_MS - (this.expiresAt - started);
          return null;
        }
        const age = Number(response.headers.get("X-Capture-Age-Ms") ?? NaN);
        const expiresAt = started + SURFACE_MAX_AGE_MS - age;
        if (
          response.ok &&
          (!Number.isFinite(age) || age < 0 || performance.now() > expiresAt)
        ) {
          await response.body?.cancel();
          this.issue = "RGB-D capture too old or invalid; scan retained";
          return null;
        }
        const buffer = await boundedBody(response, signal);
        if (performance.now() > expiresAt) return null;
        this.expiresAt = expiresAt;
        this.ageMs = age;
        this.pendingEtag = response.headers.get("ETag") ?? "";
        return { buffer, expiresAt, trackingLostCapture: -1 };
      }
    }
    const response = await this.request(`${this.base}/capture/status`, {
      signal,
      cache: "no-store",
    });
    if (!response.ok) {
      this.issue = "Capture API unavailable; check Backend API base";
      return null;
    }
    const status = record(await response.json());
    if (record(status.health).phone === "down") {
      this.issue = "Phone offline; scan retained";
      return null;
    }
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
      record(rich.metadata).tracking !== "limited" &&
      record(rich.metadata).tracking !== "not_available" &&
      (!Number.isFinite(legacyTime) || richTime >= legacyTime - 0.035);
    const frame = useRich || !legacy.capture_id ? rich : legacy;
    const token = String(
      useRich ? (frame.token ?? "") : (frame.capture_id ?? frame.token ?? ""),
    );
    const age = frame.age_ms;
    if (
      typeof age !== "number" ||
      !Number.isFinite(age) ||
      age < 0 ||
      age > SURFACE_MAX_AGE_MS
    ) {
      this.issue = !token
        ? "No RGB-D capture received"
        : "RGB-D capture too old or invalid; scan retained";
      return null;
    }
    this.ageMs = age;
    if (!useRich && !legacy.capture_id) {
      this.issue = "No usable RGB-D capture received";
      return null;
    }
    if (!token) {
      this.issue = "No RGB-D capture received";
      return null;
    }
    if (token === this.token) return null;
    const packet = await this.request(
      `${this.base}/capture/${useRich ? "rich/frame" : "frame"}.bin`,
      { signal, cache: "no-store" },
    );
    const buffer = await boundedBody(packet, signal);
    const expiresAt = started + SURFACE_MAX_AGE_MS - age;
    if (performance.now() > expiresAt) return null;
    this.pendingToken = token;
    return {
      buffer,
      expiresAt,
      trackingLostCapture: status.tracking_lost_capture ?? -1,
    };
  }
}
