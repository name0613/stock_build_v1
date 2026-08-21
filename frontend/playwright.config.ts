import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  metadata: {
    base_url: process.env.E2E_BASE_URL || "http://127.0.0.1:18080",
    acceptance_run_id: process.env.ACCEPTANCE_RUN_ID || "unset",
    source_revision: process.env.SOURCE_REVISION || "unset",
    score_version: process.env.SCORE_VERSION || "unset",
    formula_hash: process.env.FORMULA_HASH || "unset",
    frontend_image_digest: process.env.FRONTEND_IMAGE_DIGEST || "unset",
  },
  use: { baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:18080", screenshot: "only-on-failure", trace: "retain-on-failure" },
});
