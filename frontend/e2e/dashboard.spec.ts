import { test, expect } from "@playwright/test";

test("dashboard loads and exposes evidence controls", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "低調持續建倉監控" })).toBeVisible();
  await expect(page.getByLabel("股票代碼或名稱搜尋")).toBeVisible();
  await expect(page.getByRole("button", { name: "只看 Strong" })).toBeVisible();
  const health = await page.request.get("/health");
  expect(health.status()).toBe(200);
  const stocks = await page.request.get("/api/stocks?page=1&page_size=50");
  expect(stocks.status()).toBe(200);
  const stocksJson = await stocks.json();
  expect(Array.isArray(stocksJson.items)).toBeTruthy();
  expect(stocksJson.total).toBeGreaterThan(0);
});

test("search and insufficient-data state are visible", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("股票代碼或名稱搜尋").fill("2330");
  const filtered = await page.request.get("/api/stocks?page=1&page_size=50&search=2330");
  expect(filtered.status()).toBe(200);
  const filteredJson = await filtered.json();
  expect(filteredJson.items.length).toBeGreaterThan(0);
  expect(filteredJson.items.some((item: { stock_id: string }) => item.stock_id === "2330")).toBeTruthy();
  await expect(page.locator("tbody tr.clickable").filter({ hasText: "2330" })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("API 尚未可用");
});

test("NAS detail exposes S-level charts, provenance and missing-data gaps", async ({ page }) => {
  await page.goto("/");
  const detailResponse = await page.request.get("/api/stocks/2330?limit=200");
  expect(detailResponse.status()).toBe(200);
  const detailJson = await detailResponse.json();
  expect(detailJson.stock.stock_id).toBe("2330");
  expect(detailJson.sources).toBeTruthy();
  expect(detailJson.calendar_version).toMatch(/^tw-exchange-/);
  expect(detailJson.holding_series).toBeTruthy();
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
  const rankingJson = await rankings.json();
  expect(rankingJson.score_version).toBe(summaryJson.score_version);
  expect(Array.isArray(rankingJson.items)).toBeTruthy();
  const currentStocks = await page.request.get("/api/stocks?page=1&page_size=50&sort=score");
  expect(currentStocks.ok()).toBeTruthy();
  const currentStocksJson = await currentStocks.json();
  if (summaryJson.provider_state.numeric_scores_allowed === false) {
    expect(rankingJson.items.every((item: { score: number | null }) => typeof item.score === "number")).toBeTruthy();
    expect(currentStocksJson.items.every((item: { score: number | null; status: string }) => item.score === null || typeof item.score === "number")).toBeTruthy();
    const partialDetail = await page.request.get("/api/stocks/2330?limit=20");
    expect(partialDetail.ok()).toBeTruthy();
    const partialDetailJson = await partialDetail.json();
    expect(partialDetailJson.score.score === null || typeof partialDetailJson.score.score === "number").toBeTruthy();
    expect(summaryJson.data_insufficient_count).toBeGreaterThanOrEqual(0);
  } else {
    expect(summaryJson.provider_state.status).toBe("AVAILABLE");
    expect(summaryJson.provider_state.numeric_scores_allowed).toBeTruthy();
    for (const item of currentStocksJson.items) {
      expect(item.score === null || typeof item.score === "number").toBeTruthy();
    }
    for (let i = 1; i < rankingJson.items.length; i += 1) {
      expect(rankingJson.items[i - 1].score).toBeGreaterThanOrEqual(rankingJson.items[i].score);
    }
  }
  await page.getByLabel("股票代碼或名稱搜尋").fill("2330");
  await page.getByLabel("市場").selectOption("上市");
  await page.getByLabel("狀態").selectOption("DATA_INSUFFICIENT");
  const filteredApi = await page.request.get("/api/stocks?page=1&page_size=50&search=2330&market=%E4%B8%8A%E5%B8%82&status=DATA_INSUFFICIENT");
  expect(filteredApi.ok()).toBeTruthy();
  const filteredJson = await filteredApi.json();
  expect(filteredJson.items.every((item: { status: string }) => item.status === "DATA_INSUFFICIENT")).toBeTruthy();
  await expect.poll(async () => page.locator("tbody tr.clickable").count()).toBe(filteredJson.items.length);
  for (const item of filteredJson.items) await expect(page.locator("tbody tr.clickable").filter({ hasText: item.stock_id })).toBeVisible();
  await expect(page.locator("th").filter({ hasText: ">400 張" })).toBeVisible();
});
