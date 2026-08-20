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
