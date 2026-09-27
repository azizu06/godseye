import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { operatorProxy } from "./operatorProxy";
export default defineConfig(({ mode }) => {
  const { GODSEYE_BACKEND_URL, GODSEYE_ROVER_KEY_FILE } = loadEnv(
    mode,
    process.cwd(),
    "GODSEYE_",
  );
  let target: string | undefined;
  if (GODSEYE_BACKEND_URL) {
    const backend = new URL(GODSEYE_BACKEND_URL);
    if (
      !["http:", "https:"].includes(backend.protocol) ||
      backend.username ||
      backend.password ||
      backend.pathname !== "/" ||
      backend.search ||
      backend.hash
    )
      throw Error("GODSEYE_BACKEND_URL must be an HTTP(S) backend origin");
    target = backend.origin;
  }
  // Only the configured backend and viewer routes can be reached through this relay.
  const proxy = target
    ? {
        "^/live(?:\\?|$)": { target, ws: true },
        "^/capture(?:/|\\?|$)": { target },
        "^/health(?:\\?|$)": { target },
        "^/autonomy$": { target },
        "^/device(?:/action)?$": { target },
        // Motion endpoints still require explicit UI enable and backend pairing.
        "^/(?:arm|stop|mode|manual|goal|session|rescan)(?:\\?|$)": { target },
        // Read-only suggested walking route; it cannot set a goal or move the rover.
        "^/route$": { target },
      }
    : undefined;
  return {
    plugins: [
      react(),
      operatorProxy(target ? GODSEYE_ROVER_KEY_FILE : undefined),
    ],
    test: { include: ["src/**/*.test.ts"] },
    server: { proxy },
    preview: { proxy },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            three: ["three", "@react-three/fiber", "@react-three/drei"],
          },
        },
      },
    },
  };
});
