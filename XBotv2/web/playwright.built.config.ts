import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "built-*.spec.ts",
  reporter: "line",
  use: {
    baseURL: process.env.XBOT_BUILT_WEB_URL || "http://127.0.0.1:4174",
    trace: "retain-on-failure",
    viewport: { width: 1440, height: 960 },
  },
});
