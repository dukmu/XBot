import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "real-*.spec.ts",
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:4174",
    trace: "retain-on-failure",
    viewport: { width: 1440, height: 960 },
  },
  webServer: [
    {
      command: "PYTHONPATH=../.. ../../.venv/bin/python e2e/real_server.py",
      url: "http://127.0.0.1:4097/health",
      reuseExistingServer: false,
    },
    {
      command: "XBOT_API_URL=http://127.0.0.1:4097 npm run dev -- --host 127.0.0.1 --port 4174",
      url: "http://127.0.0.1:4174",
      reuseExistingServer: false,
    },
  ],
});
