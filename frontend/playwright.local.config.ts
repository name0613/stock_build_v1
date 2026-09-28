import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// Hermetic browser tests: all API responses are supplied by the specs.
export default defineConfig({
  ...base,
  testMatch: ["refresh-exclusions.spec.ts", "capital-aware.spec.ts", "deterministic.spec.ts"],
  use: { ...base.use, baseURL: "http://127.0.0.1:18081" },
  webServer: { command: "npm run dev -- --host 127.0.0.1 --port 18081 --strictPort", url: "http://127.0.0.1:18081", reuseExistingServer: false },
});
