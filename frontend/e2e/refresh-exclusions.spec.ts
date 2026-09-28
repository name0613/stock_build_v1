import { expect, Page, test } from "@playwright/test";

async function fixtures(page: Page, count = 257) {
  const state = { status: "", posts: 0, fail: false, delay: 0, outcome: "SUCCESS", fallback: false, summaryReads: 0 };
  const all = Array.from({ length: count }, (_, index) => ({ stock_id: String(7000 + index), stock_name: `測試股票${index}`, market: index % 2 ? "上櫃" : "上市", no_data_attempts: 5, attempt_limit: 5, reason: index % 2 ? "INCOMPLETE" : "NO_DATA", missing_sources: index % 2 ? ["TaiwanStockPrice"] : null, last_attempt_at: "2026-09-08T13:00:00Z" }));
  const terminal = () => ["SUCCESS", "DATA_INSUFFICIENT", "FAILED"].includes(state.status);
  const job = () => ({ job_id: 501, stock_id: "7000", status: state.status, phase: state.status === "RUNNING" ? "fetching:TaiwanStockPrice" : "queued", progress: { completed: 2, total: 5 }, target_date: "2026-09-08", evaluated_source_date: state.fallback ? "2026-09-07" : "2026-09-08", fallback_applied: state.fallback, score: { score: state.status === "SUCCESS" ? 82.5 : null }, target_readiness: { missing_reasons: state.fallback || state.status === "DATA_INSUFFICIENT" ? ["missing_broker"] : [] }, error_code: state.status === "FAILED" ? "ACCESS_DENIED" : undefined, recovery: { automatic_refresh_eligibility: terminal() ? "RESTORED" : "EXCLUDED_UNTIL_FINISHED", released_at: terminal() ? "2026-09-08T14:00:00Z" : null } });
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/refresh-exclusions") {
      if (state.delay) await new Promise(resolve => setTimeout(resolve, state.delay));
      if (state.fail) return route.fulfill({ status: 503, json: {} });
      const excluded = all.filter(item => item.stock_id !== "7000" || !terminal());
      const filtered = excluded.filter(item => (!url.searchParams.get("search") || `${item.stock_id}${item.stock_name}`.includes(url.searchParams.get("search")!)) && (!url.searchParams.get("market") || item.market === url.searchParams.get("market")));
      const number = Number(url.searchParams.get("page") || 1), size = Number(url.searchParams.get("page_size") || 50);
      return route.fulfill({ json: { total: excluded.length, filtered_total: filtered.length, items: filtered.slice((number - 1) * size, number * size).map(item => ({ ...item, job: item.stock_id === "7000" && state.status && !terminal() ? job() : null })), active_jobs: state.status && !terminal() ? [job()] : [], recent_results: terminal() ? [job()] : [], daily_completion: { target_date: "2026-09-08", pending_count: terminal() ? 1 : 0, completed_count: 0, excluded_count: excluded.length } } });
    }
    if (url.pathname === "/api/refresh-exclusions/7000/recover") {
      state.posts++; state.status = "QUEUED";
      await new Promise(resolve => setTimeout(resolve, 180));
      return route.fulfill({ status: 202, json: job() });
    }
    if (url.pathname === "/api/stocks/7000/fetch-and-score") return route.fulfill({ json: job() });
    if (url.pathname === "/api/summary") { state.summaryReads++; return route.fulfill({ json: { stock_count: 257, strong_count: 0, accumulation_count: 0, watch_count: 0, data_insufficient_count: 257, sync_status: [] } }); }
    if (url.pathname === "/api/stocks") return route.fulfill({ json: { total: 257, items: [] } });
    if (url.pathname === "/api/rankings") return route.fulfill({ json: { kind: url.searchParams.get("kind"), score_version: "s-only-v6", items: [] } });
    if (url.pathname === "/api/holdings/status") return route.fulfill({ json: { total: 257, available_count: 0, items: [] } });
    return route.fulfill({ status: 404, json: {} });
  });
  return state;
}

test("button follows high confidence; return preserves ranking, filters and page", async ({ page }, testInfo) => {
  await fixtures(page);
  await page.goto("/");
  const tabs = page.getByTestId("ranking-tabs");
  await expect(tabs.locator("button")).toHaveText(["隱性建倉", "大型資金建倉", "高可信建倉", "排除股票（257）"]);
  const last = await tabs.locator("button").nth(3).boundingBox();
  const prior = await tabs.locator("button").nth(2).boundingBox();
  expect(last!.x).toBeGreaterThan(prior!.x);
  await page.getByRole("button", { name: "隱性建倉", exact: true }).click();
  await page.getByRole("textbox", { name: "股票代碼或名稱搜尋" }).fill("保留搜尋");
  await page.locator(".control-row select").first().selectOption("上市");
  await page.getByRole("button", { name: "下一頁 →" }).click();
  await page.getByTestId("refresh-exclusions-button").click();
  await expect(page.getByRole("heading", { name: "自動補抓排除清單" })).toBeVisible();
  await expect(page.getByTestId("exclusion-row-7000")).toContainText("完全無資料");
  await expect(page.getByTestId("exclusion-row-7000")).toContainText("未記錄");
  await expect(page.getByTestId("exclusion-row-7001")).toContainText("必要來源不完整");
  await page.screenshot({ path: testInfo.outputPath("exclusions-desktop.png"), fullPage: false });
  await page.getByRole("button", { name: "返回原榜單", exact: false }).click();
  await expect(page.getByRole("button", { name: "隱性建倉", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("textbox", { name: "股票代碼或名稱搜尋" })).toHaveValue("保留搜尋");
  await expect(page.locator(".control-row select").first()).toHaveValue("上市");
  await expect(page.locator(".pagination")).toContainText("第 2 頁");
});

test("server pagination reaches beyond 200 and search/market use full list", async ({ page }) => {
  await fixtures(page);
  await page.goto("/#refresh-exclusions");
  const seen = new Set<string>();
  for (let number = 1; number <= 6; number++) {
    await expect(page.locator(".pagination")).toContainText(`第 ${number} / 6 頁`);
    for (const id of await page.locator('[data-testid^="exclusion-row-"]').evaluateAll(rows => rows.map(row => row.getAttribute("data-testid")!))) seen.add(id);
    if (number < 6) await page.getByRole("button", { name: "下一頁", exact: true }).click();
  }
  expect(seen.size).toBe(257);
  await page.getByLabel("排除股票代碼或名稱").fill("7256");
  await expect(page.getByTestId("exclusion-count")).toContainText("篩選結果 1 檔");
  await expect(page.getByTestId("exclusion-row-7256")).toBeVisible();
  await page.getByLabel("排除股票市場").selectOption("上櫃");
  await expect(page.getByText("查無符合搜尋或市場篩選的排除股票。")).toBeVisible();
  await page.getByLabel("排除股票代碼或名稱").fill("");
  await expect(page.getByTestId("exclusion-count")).toContainText("篩選結果 128 檔");
});

for (const outcome of ["SUCCESS", "DATA_INSUFFICIENT", "FAILED"]) {
  test(`queue/wait/reload then ${outcome} keeps receipt after removal`, async ({ page }) => {
    const state = await fixtures(page, 2);
    await page.goto("/#refresh-exclusions");
    const row = page.getByTestId("exclusion-row-7000");
    const button = row.getByRole("button");
    await button.click();
    await expect(button).toBeDisabled();
    await expect(row).toContainText("排隊中");
    expect(state.posts).toBe(1);
    for (const status of ["RUNNING", "WAITING_FOR_QUOTA", "WAITING_FOR_PROVIDER"]) {
      state.status = status;
      await page.getByRole("button", { name: "重新讀取", exact: true }).click();
      await expect(row).toContainText(status === "RUNNING" ? "補抓：股價／成交量" : status === "WAITING_FOR_QUOTA" ? "等待額度" : "等待供應商");
      await expect(row).toContainText("仍排除，作業結束後解除");
    }
    await page.reload();
    await expect(row).toContainText("等待供應商");
    await expect(row.getByRole("button")).toBeDisabled();
    const previousReads = state.summaryReads;
    state.status = outcome;
    state.fallback = outcome === "SUCCESS";
    await page.getByRole("button", { name: "重新讀取", exact: true }).click();
    await expect(row).toHaveCount(0);
    const summary = page.getByRole("region", { name: "復原結果摘要" });
    await expect(summary).toContainText("自動補抓資格：已恢復");
    await expect(summary).toContainText(outcome === "SUCCESS" ? "Score 82.5" : outcome === "FAILED" ? "最終失敗" : "DATA_INSUFFICIENT");
    if (outcome === "SUCCESS") {
      await expect(summary).toContainText("實際評分日 2026-09-07");
      await expect(summary).toContainText("最新目標日尚未完成");
    }
    await expect.poll(() => state.summaryReads).toBeGreaterThan(previousReads);
    await expect(page.getByTestId("exclusion-count")).toContainText("全體排除 1 檔");
    await page.reload();
    await expect(summary).toContainText("自動補抓資格：已恢復");
    await page.getByRole("button", { name: "返回原榜單", exact: false }).click();
    await expect(page.getByTestId("refresh-exclusions-button")).toHaveText("排除股票（1）");
  });
}

test("loading, read failure, retry and empty state", async ({ page }) => {
  const state = await fixtures(page, 0);
  state.delay = 300;
  state.fail = true;
  await page.goto("/#refresh-exclusions");
  await expect(page.getByText("正在載入排除清單…")).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("讀取失敗");
  state.fail = false;
  await page.getByRole("button", { name: "重新讀取", exact: true }).click();
  await expect(page.getByText("目前沒有被排除的股票。")).toBeVisible();
});

test("ranking buttons wrap on narrow screens", async ({ page }) => {
  await fixtures(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const tabs = page.getByTestId("ranking-tabs");
  await expect(tabs.locator("button")).toHaveCount(4);
  const boxes = await tabs.locator("button").evaluateAll(buttons => buttons.map(button => {
    const r = button.getBoundingClientRect(); return { x: r.x, y: r.y, right: r.right };
  }));
  expect(boxes[3].y).toBeGreaterThan(boxes[0].y);
  expect(boxes.every(box => box.x >= 0 && box.right <= 390)).toBe(true);
  await page.getByTestId("refresh-exclusions-button").click();
  await expect(page.getByRole("heading", { name: "自動補抓排除清單" })).toBeVisible();
});
