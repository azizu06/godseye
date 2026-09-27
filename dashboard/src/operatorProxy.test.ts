import { expect, it } from "vitest";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";
import { operatorProxy } from "../operatorProxy";

it("pairs a fresh browser without exposing the key and rejects cross-origin commands", async () => {
  const folder = mkdtempSync(join(tmpdir(), "godseye-proxy-test-"));
  const key = "ONLY_A_TEST_KEY_NOT_REAL_0123456789";
  writeFileSync(join(folder, "key"), key);
  let handle!: (
    req: IncomingMessage,
    res: ServerResponse,
    next: () => void,
  ) => void;
  const plugin = operatorProxy(join(folder, "key"));
  (plugin.configureServer as Function)({
    middlewares: {
      use: (fn: typeof handle) => {
        handle = fn;
      },
    },
  });
  const server = createServer((req, res) =>
    handle(req, res, () => {
      res.end(
        req.headers.authorization === `Bearer ${key}` ? "paired" : "unpaired",
      );
    }),
  );
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address() as { port: number };
  const base = `http://127.0.0.1:${address.port}`;
  try {
    const status = await (await fetch(`${base}/operator/status`)).text();
    expect(JSON.parse(status)).toEqual({ paired: true });
    expect(status).not.toContain(key);
    const headers = { Origin: base, "Content-Type": "application/json" };
    expect(
      await (
        await fetch(`${base}/arm?prepare=true`, { method: "POST", headers })
      ).text(),
    ).toBe("paired");
    expect(
      (
        await fetch(`${base}/arm`, {
          method: "POST",
          headers: { ...headers, Origin: "https://other.example" },
        })
      ).status,
    ).toBe(403);
    expect((await fetch(`${base}/arm`, { method: "POST" })).status).toBe(403);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
    rmSync(folder, { recursive: true, force: true });
  }
});
