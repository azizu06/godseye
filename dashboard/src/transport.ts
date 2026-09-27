export interface ConnectionConfig {
  source: "external";
  wsUrl: string;
  apiUrl: string;
  commands: boolean;
  /** In-memory only. Never persisted in feed URLs or exports. */
  roverKey?: string;
}
export const defaultConfig: ConnectionConfig = {
  source: "external",
  wsUrl: "ws://localhost:8765/live",
  apiUrl: "http://localhost:8765",
  commands: false,
};
/** URL-selected feeds survive reload without persisting control permission. */
export function initialConfig(
  search = window.location.search,
): ConnectionConfig {
  const params = new URLSearchParams(search);
  const live = params.get("live") ?? import.meta.env.VITE_LIVE_URL;
  if (!live) return { ...defaultConfig };
  try {
    const ws = new URL(
      live,
      typeof window === "undefined" ? undefined : window.location.href,
    );
    if (ws.protocol === "http:") ws.protocol = "ws:";
    if (ws.protocol === "https:") ws.protocol = "wss:";
    const api = new URL(ws);
    api.protocol = ws.protocol === "wss:" ? "https:" : "http:";
    api.pathname = "/";
    api.search = "";
    api.hash = "";
    const config = {
      ...defaultConfig,
      wsUrl: ws.href,
      apiUrl: params.get("api") ?? api.origin,
    };
    return validateConfig(config) ? { ...defaultConfig } : config;
  } catch {
    return { ...defaultConfig };
  }
}
export function updateFeedUrl(config: ConnectionConfig) {
  const url = new URL(window.location.href);
  url.searchParams.set("live", config.wsUrl);
  url.searchParams.set("api", config.apiUrl);
  window.history.replaceState(null, "", url);
}
export function validateConfig(config: ConnectionConfig): string | null {
  try {
    if (!["ws:", "wss:"].includes(new URL(config.wsUrl).protocol))
      return "Use a ws:// or wss:// telemetry address.";
  } catch {
    return "Enter a valid WebSocket address.";
  }
  try {
    if (!["http:", "https:"].includes(new URL(config.apiUrl).protocol))
      return "Use an http:// or https:// API address.";
  } catch {
    return "Enter a valid API address.";
  }
  return null;
}
export async function sendCommand(
  base: string,
  path: string,
  body?: Record<string, unknown>,
  signal?: AbortSignal,
  roverKey?: string,
) {
  const response = await fetch(`${base.replace(/\/$/, "")}${path}`, {
    method: "POST",
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(roverKey ? { Authorization: `Bearer ${roverKey}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: signal ?? AbortSignal.timeout(4000),
  });
  let data: Record<string, unknown> = {};
  try {
    data = await response.json();
  } catch {
    /* HTTP status still determines success. */
  }
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : response.status === 501
          ? "This feature is not implemented by the connected backend."
          : `Command rejected (${response.status}).`,
    );
  return data;
}
export class ManualController {
  private timer: ReturnType<typeof setInterval> | null = null;
  private busy = false;
  private generation = 0;
  private active = false;
  private abort: AbortController | null = null;
  constructor(
    private send: (
      body: Record<string, number>,
      signal?: AbortSignal,
    ) => Promise<unknown>,
    private error: (error: unknown) => void,
  ) {}
  start(v: number, w: number) {
    this.stop();
    this.active = true;
    const generation = ++this.generation;
    const pulse = async () => {
      if (this.busy || !this.active || generation !== this.generation) return;
      this.busy = true;
      this.abort = new AbortController();
      const timeout = setTimeout(() => this.abort?.abort(), 800);
      try {
        await this.send({ v_mps: v, yaw_rate_rps: w }, this.abort.signal);
      } catch (e) {
        if (generation === this.generation) {
          this.stop();
          this.error(e);
        }
      } finally {
        clearTimeout(timeout);
        this.busy = false;
      }
    };
    void pulse();
    this.timer = setInterval(() => void pulse(), 100);
  }
  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.generation++;
    this.abort?.abort();
    this.abort = null;
    if (this.active) {
      this.active = false;
      void this.send({ v_mps: 0, yaw_rate_rps: 0 }).catch(() => {});
    }
  }
}
