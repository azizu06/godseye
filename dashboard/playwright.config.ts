import { defineConfig } from "@playwright/test";
const port = Number(process.env.GODSEYE_DASHBOARD_TEST_PORT ?? 5173);
export default defineConfig({
  testDir: "./tests",
  timeout: 60000,
  workers: 1,
  expect: { timeout: 10000 },
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    viewport: { width: 1440, height: 1100 },
    headless: true,
    channel: "chromium",
    launchOptions: {
      args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
    },
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `npm run dev -- --port ${port} --strictPort`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: true,
  },
  reporter: "list",
});
