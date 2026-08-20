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

