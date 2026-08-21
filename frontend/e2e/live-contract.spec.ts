import { expect, test } from "@playwright/test";

const statuses = ["STRONG_ACCUMULATION", "ACCUMULATION", "WATCH", "DATA_INSUFFICIENT", "NO_STRONG_EVIDENCE"];

test("live health build metadata score specification and summary are coherent", async ({ page }, testInfo) => {
  const started = performance.now();
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "低調持續建倉監控" })).toBeVisible();
  await expect(page.locator("body")).not.toContainText("API 尚未可用");
  await expect(page.getByTestId("stock-row").first()).toBeVisible();
  const dataReadyMs = Math.round((performance.now() - started) * 100) / 100;
  testInfo.annotations.push({ type: "lan_data_ready_ms", description: String(dataReadyMs) });
  testInfo.annotations.push({ type: "lan_data_ready_budget_ms", description: "2000" });
  expect(dataReadyMs).toBeLessThanOrEqual(2000);

  const [healthResponse, buildResponse, specResponse, summaryResponse] = await Promise.all([
    page.request.get("/health"),
    page.request.get("/api/build-metadata"),
    page.request.get("/api/score-spec"),
    page.request.get("/api/summary"),
  ]);
  expect(healthResponse.status()).toBe(200);
  expect(buildResponse.status()).toBe(200);
  expect(specResponse.status()).toBe(200);
  expect(summaryResponse.status()).toBe(200);
  const [health, build, spec, summary] = await Promise.all([healthResponse.json(), buildResponse.json(), specResponse.json(), summaryResponse.json()]);
  expect(build.build_metadata_available).toBe(true);
  expect(build.source_revision).toMatch(/^[a-f0-9]{40}$/);
  expect(build.score_spec_match).toBe(true);
  expect(build.calendar_match).toBe(true);
  expect(health.score_version).toBe(spec.score_version);
  expect(health.formula_hash).toBe(spec.formula_hash);
  expect(summary.score_version).toBe(spec.score_version);
  expect(summary.formula_hash).toBe(spec.formula_hash);
  expect(spec.formula_hash).toMatch(/^[a-f0-9]{64}$/);
  expect(spec.spec.weights).toEqual({ institutional_persistence: 0.35, ownership_accumulation: 0.35, broker_persistence: 0.3, base_sum: 1 });
  expect(summary.status_invariant).toBe(true);
  expect(summary.strong_count + summary.accumulation_count + summary.watch_count + summary.data_insufficient_count + summary.no_strong_evidence_count).toBe(summary.stock_count);
});

test("live list filters pagination ranking and numeric-null ordering obey the API contract", async ({ page }) => {
  const summaryResponse = await page.request.get("/api/summary");
  expect(summaryResponse.status()).toBe(200);
  const summary = await summaryResponse.json();
  const firstResponse = await page.request.get("/api/stocks?page=1&page_size=50&sort=score&order=desc");
  expect(firstResponse.status()).toBe(200);
  const first = await firstResponse.json();
  expect(first.total).toBe(summary.stock_count);
  expect(first.items.length).toBeGreaterThan(0);

  const statusResponses = await Promise.all(statuses.map((status) => page.request.get(`/api/stocks?page=1&page_size=1&status=${status}`)));
  expect(statusResponses.every((response) => response.status() === 200)).toBe(true);
  const statusPages = await Promise.all(statusResponses.map((response) => response.json()));
  expect(statusPages.reduce((total, result) => total + result.total, 0)).toBe(summary.stock_count);
  statusPages.forEach((result, index) => expect(result.items.every((item: { status: string }) => item.status === statuses[index])).toBe(true));

  const sample = first.items[0];
  const marketResponse = await page.request.get(`/api/stocks?page=1&page_size=50&market=${encodeURIComponent(sample.market)}`);
  const searchResponse = await page.request.get(`/api/stocks?page=1&page_size=50&search=${encodeURIComponent(sample.stock_id)}`);
  expect(marketResponse.status()).toBe(200);
  expect(searchResponse.status()).toBe(200);
  const [marketPage, searchPage] = await Promise.all([marketResponse.json(), searchResponse.json()]);
  expect(marketPage.items.every((item: { market: string }) => item.market === sample.market)).toBe(true);
  expect(searchPage.items.some((item: { stock_id: string }) => item.stock_id === sample.stock_id)).toBe(true);

  const minimumResponse = await page.request.get("/api/stocks?page=1&page_size=50&sort=score&order=desc&min_score=0");
  expect(minimumResponse.status()).toBe(200);
  const minimumPage = await minimumResponse.json();
  if (summary.provider_state.numeric_scores_allowed === false) {
    expect(minimumPage.total).toBe(0);
    expect(minimumPage.items).toEqual([]);
  } else {
    expect(minimumPage.items.every((item: { score: number | null }) => typeof item.score === "number" && item.score >= 0)).toBe(true);
  }

  const secondResponse = await page.request.get("/api/stocks?page=2&page_size=50&sort=score&order=desc");
  expect(secondResponse.status()).toBe(200);
  const second = await secondResponse.json();
  const orderedScores = [...first.items, ...second.items].map((item: { score: number | null }) => item.score);
  let nullSeen = false;
  let previous = Number.POSITIVE_INFINITY;
  for (const score of orderedScores) {
    if (score == null) {
      nullSeen = true;
      continue;
    }
    expect(nullSeen).toBe(false);
    expect(score).toBeLessThanOrEqual(previous);
    previous = score;
  }

  const rankingResponse = await page.request.get("/api/rankings?kind=top&limit=200");
  expect(rankingResponse.status()).toBe(200);
  const ranking = await rankingResponse.json();
  expect(ranking.score_version).toBe(summary.score_version);
  expect(ranking.items.every((item: { score: number | null }) => typeof item.score === "number")).toBe(true);
  for (let index = 1; index < ranking.items.length; index += 1) expect(ranking.items[index - 1].score).toBeGreaterThanOrEqual(ranking.items[index].score);
});

test("live detail renders exact provenance formula unavailable policy broker caveat and chart segments", async ({ page }) => {
  const listResponse = await page.request.get("/api/stocks?page=1&page_size=1&sort=stock_id&order=asc");
  expect(listResponse.status()).toBe(200);
  const list = await listResponse.json();
  expect(list.items).toHaveLength(1);
  const stockId = list.items[0].stock_id;
  const detailResponse = await page.request.get(`/api/stocks/${stockId}?limit=200`);
  expect(detailResponse.status()).toBe(200);
  const detail = await detailResponse.json();
  expect(detail.stock.stock_id).toBe(stockId);
  expect(detail.calendar_version).toMatch(/^tw-exchange-/);
  expect(Object.keys(detail.sources).sort()).toEqual(["broker", "foreign_holding", "holding_distribution", "institutional", "major_shareholder_5pct", "price"]);
  expect(detail.sources.major_shareholder_5pct.status).toBe("UNAVAILABLE_NOT_CONFIGURED");
  expect(detail.score.formula_hash).toMatch(/^[a-f0-9]{64}$/);

  await page.goto("/");
  await page.getByLabel("股票代碼或名稱搜尋").fill(stockId);
  const row = page.getByTestId("stock-row").filter({ hasText: stockId }).first();
  await expect(row).toBeVisible();
  await row.click();
  await expect(page.getByText("Final = institutional 35% + ownership 35% + broker 30% + low-profile modifier。", { exact: false })).toBeVisible();
  await expect(page.getByText(`Formula hash ${detail.score.formula_hash}`)).toBeVisible();
  await expect(page.getByText("v6 只計入逐列驗證的正買超事件，未出現分點保持 unknown，絕不補零。", { exact: false })).toBeVisible();
  await expect(page.getByTestId("source-major_shareholder_5pct")).toContainText("UNAVAILABLE_NOT_CONFIGURED");
  await expect(page.getByText(/分點資料 unavailable|持續承接證據/).first()).toBeVisible();

  const expectedSegments = ["400", "1000"].reduce((total, key) => {
    const values = (detail.holding_series[key] || []).map((point: { value: number | null }) => point.value);
    let run = 0;
    let segments = 0;
    values.forEach((value: number | null) => {
      if (value == null || !Number.isFinite(Number(value))) {
        if (run > 1) segments += 1;
        run = 0;
      } else run += 1;
    });
    if (run > 1) segments += 1;
    return total + segments;
  }, 0);
  const holdingChart = page.getByRole("region", { name: ">400／>1000 lots 持股比例" });
  if (expectedSegments === 0) await expect(holdingChart).toContainText("資料不足，無法繪製");
  else await expect(holdingChart.locator("polyline")).toHaveCount(expectedSegments);
});
