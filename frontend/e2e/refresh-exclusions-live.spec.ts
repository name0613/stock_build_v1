import { expect, test } from "@playwright/test";

// Read-only deployment smoke test; never submits recovery or provider work.
test.skip(process.env.EXCLUSIONS_LIVE_SMOKE !== "true", "requires an explicitly selected deployed server");

test("deployed exclusion page reads real API data and preserves navigation", async ({ page, request }) => {
  const response = await request.get("/api/refresh-exclusions?page_size=50");
  expect(response.ok()).toBeTruthy();
  const result = await response.json();
  expect(result.total).toBeGreaterThanOrEqual(result.items.length);
  expect(result.filtered_total).toBe(result.total);
  expect(result.items.length).toBeLessThanOrEqual(50);
  for (const item of result.items) expect(item.no_data_attempts).toBeGreaterThanOrEqual(item.attempt_limit);
  await page.goto("/");
  const tabs = page.getByTestId("ranking-tabs");
  await expect(tabs.locator("button").nth(2)).toHaveText("高可信建倉");
  await expect(tabs.locator("button").nth(3)).toContainText("排除股票");
  await tabs.locator("button").nth(3).click();
  await expect(page.getByRole("heading", { name: "自動補抓排除清單" })).toBeVisible();
  await expect(page.getByTestId("exclusion-count")).toContainText(`全體排除 ${result.total} 檔`);
  if (result.items.length) {
    const stock = result.items[0];
    await expect(page.getByTestId(`exclusion-row-${stock.stock_id}`)).toContainText(stock.stock_name);
    await page.getByLabel("排除股票代碼或名稱").fill(stock.stock_id);
    await expect(page.getByTestId(`exclusion-row-${stock.stock_id}`)).toBeVisible();
  }
  await page.reload();
  await expect(page.getByRole("heading", { name: "自動補抓排除清單" })).toBeVisible();
  await page.getByRole("button", { name: "返回原榜單", exact: false }).click();
  await expect(tabs).toBeVisible();
});
