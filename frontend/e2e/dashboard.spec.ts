import { test, expect } from "@playwright/test";

test("dashboard loads and exposes evidence controls", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "低調持續建倉監控" })).toBeVisible();
  await expect(page.getByLabel("股票代碼或名稱搜尋")).toBeVisible();
  await expect(page.getByRole("button", { name: "只看 Strong" })).toBeVisible();
});

test("search and insufficient-data state are visible", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("股票代碼或名稱搜尋").fill("2330");
  await expect(page.locator("body")).toContainText(/全部普通股|DATA_INSUFFICIENT|API 尚未可用/);
});

test("NAS detail exposes S-level charts, provenance and missing-data gaps", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("股票代碼或名稱搜尋").fill("2330");
  const row = page.locator("tbody tr.clickable").first();
  await expect(row).toBeVisible();
  await row.click();
  await expect(page.getByText("來源與更新時間")).toBeVisible();
  await expect(page.getByText(/TaiwanStockInstitutionalInvestorsBuySellWide|TaiwanStockShareholding/).first()).toBeVisible();
  await expect(page.getByRole("img", { name: "資料趨勢圖" }).first()).toBeVisible();
  await expect(page.getByText(/不等同於一位自然人|分點資料 unavailable/)).toBeVisible();
});

test("filters, rankings, freshness and score hash are exposed", async ({ page }) => {
  await page.goto("/");
  const summary = await page.request.get("/api/summary");
  expect(summary.ok()).toBeTruthy();
  const summaryJson = await summary.json();
  expect(summaryJson.status_invariant).toBeTruthy();
  expect(summaryJson.formula_hash).toMatch(/^[a-f0-9]{64}$/);
  const rankings = await page.request.get("/api/rankings?kind=top&limit=10");
  expect(rankings.ok()).toBeTruthy();
  expect((await rankings.json()).score_version).toBe(summaryJson.score_version);
  await page.getByLabel("股票代碼或名稱搜尋").fill("2330");
  await page.getByLabel("市場").selectOption("上市");
  await page.getByLabel("狀態").selectOption("DATA_INSUFFICIENT");
  await expect(page.locator("tbody tr.clickable").first()).toBeVisible();
});
