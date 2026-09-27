import { readFileSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin } from "vite";

/** Server-side pairing for browsers using this explicitly configured dashboard. */
export function operatorProxy(keyFile?: string): Plugin {
  const key = keyFile ? readFileSync(keyFile, "utf8").trim() : "";
  if (key && !/^[A-Za-z0-9_-]{32,128}$/.test(key))
    throw Error("Invalid rover key file");
  const middleware = (
    req: IncomingMessage,
    res: ServerResponse,
    next: () => void,
  ) => {
    const path = (req.url ?? "").split("?")[0];
    if (path === "/operator/status") {
      res.setHeader("Content-Type", "application/json");
      res.setHeader("Cache-Control", "no-store");
      res.end(JSON.stringify({ paired: !!key }));
      return;
    }
    if (
      key &&
      req.method === "POST" &&
      /^\/(arm|stop|mode|manual|goal|device\/action)$/.test(path)
    ) {
      // The key never reaches JavaScript, URLs, logs or browser storage.
      // Only an explicit same-origin JSON request can use this local pairing.
      let sameOrigin = false;
      try {
        sameOrigin =
          new URL(req.headers.origin ?? "").host === req.headers.host;
      } catch {}
      if (
        !sameOrigin ||
        !req.headers["content-type"]?.startsWith("application/json")
      ) {
        res.statusCode = 403;
        res.end("Use the dashboard to send rover commands");
        return;
      }
      req.headers.authorization = `Bearer ${key}`;
    }
    next();
  };
  return {
    name: "godseye-operator-pairing",
    configureServer(server) {
      server.middlewares.use(middleware);
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware);
    },
  };
}
