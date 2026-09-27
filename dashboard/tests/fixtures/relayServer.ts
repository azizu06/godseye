import {
  createServer as httpServer,
  type IncomingHttpHeaders,
} from "node:http";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createServer, preview } from "vite";

export const TEST_ROVER_KEY = "ONLY_A_TEST_KEY_NOT_REAL_0123456789";
export interface RelayedRequest {
  url: string;
  method: string;
  headers: IncomingHttpHeaders;
  body: Buffer;
}

/** Actual Vite relay with a local recording backend; never contacts a rover/provider. */
export async function relayServer(mode: "dev" | "preview", paired = true) {
  const folder = mkdtempSync(join(tmpdir(), "godseye-relay-"));
  const received: RelayedRequest[] = [];
  const backend = httpServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    received.push({
      url: req.url!,
      method: req.method!,
      headers: req.headers,
      body: Buffer.concat(chunks),
    });
    res.setHeader("X-Relay-Fixture", "backend");
    if (req.url?.includes("fail=true")) {
      res.writeHead(409, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ detail: "Map changed" }));
    } else if (req.url?.startsWith("/audio/")) {
      res.writeHead(206, {
        "Content-Type": "audio/wav",
        "Content-Range": "bytes 0-3/4",
      });
      res.end(Buffer.from([82, 73, 70, 70]));
    } else {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ version: 1, status: "ready", events: [] }));
    }
  });
  await new Promise<void>((done) => backend.listen(0, "127.0.0.1", done));
  const backendUrl = `http://127.0.0.1:${(backend.address() as { port: number }).port}`;
  writeFileSync(join(folder, "key"), TEST_ROVER_KEY);
  const outDir = join(folder, "dist");
  mkdirSync(outDir);
  writeFileSync(
    join(outDir, "index.html"),
    "<!doctype html><title>Dashboard fallback</title>",
  );
  const previous = {
    backend: process.env.GODSEYE_BACKEND_URL,
    key: process.env.GODSEYE_ROVER_KEY_FILE,
    live: process.env.VITE_LIVE_URL,
  };
  process.env.GODSEYE_BACKEND_URL = backendUrl;
  process.env.GODSEYE_ROVER_KEY_FILE = paired ? join(folder, "key") : "";
  process.env.VITE_LIVE_URL = "/live";
  try {
    const config = {
      root: process.cwd(),
      configFile: resolve("vite.config.ts"),
      cacheDir: join(folder, "vite-cache"),
      logLevel: "silent" as const,
      server: { host: "127.0.0.1", port: 0 },
      preview: { host: "127.0.0.1", port: 0 },
      build: { outDir },
    };
    const server =
      mode === "dev" ? await createServer(config) : await preview(config);
    if ("listen" in server) await server.listen();
    const http = server.httpServer!;
    const base = `http://127.0.0.1:${(http.address() as { port: number }).port}`;
    return {
      base,
      backendUrl,
      received,
      async close() {
        if ("close" in server) await server.close();
        else await new Promise<void>((done) => http.close(() => done()));
        backend.closeAllConnections();
        await new Promise<void>((done) => backend.close(() => done()));
        rmSync(folder, { recursive: true, force: true });
      },
    };
  } catch (error) {
    backend.closeAllConnections();
    await new Promise<void>((done) => backend.close(() => done()));
    rmSync(folder, { recursive: true, force: true });
    throw error;
  } finally {
    for (const [key, value] of Object.entries({
      GODSEYE_BACKEND_URL: previous.backend,
      GODSEYE_ROVER_KEY_FILE: previous.key,
      VITE_LIVE_URL: previous.live,
    })) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}
