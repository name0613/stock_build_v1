import { expect, Page, test } from "@playwright/test";

const formulaHash = "a".repeat(64);
const coverage = {
  InstitutionalDataAvailable: true,
  ForeignHoldingDataAvailable: true,
  HoldingDistributionAvailable: true,
  BrokerDataAvailable: true,
  PriceDataAvailable: true,
};

type FixtureStock = {
  stock_id: string;
  stock_name: string;
  market: string;
  industry: string;
  price: number;
  price_change: number;
  score: number | null;
  status: string;
  score_version: string;
  features: Record<string, number>;
  coverage: Record<string, boolean>;
  latest_data: string;
};

const stocks: FixtureStock[] = Array.from({ length: 55 }, (_, index) => {
  const score = index < 52 ? 100 - index : null;
  const status = score == null ? "DATA_INSUFFICIENT" : score >= 80 ? "STRONG_ACCUMULATION" : score >= 65 ? "ACCUMULATION" : score >= 50 ? "WATCH" : "NO_STRONG_EVIDENCE";
  return {
    stock_id: String(1000 + index),
    stock_name: `Fixture ${index}`,
    market: index % 2 === 0 ? "上市" : "上櫃",
    industry: "測試產業",
    price: 100 + index,
    price_change: index / 10,
    score,
    status,
    score_version: "s-only-v6",
    features: { ForeignNet5D: 5, ForeignNet20D: 20, InvestmentTrustNet5D: 3, InvestmentTrustNet20D: 12, ForeignShareRatioChange20D: 0.2, LargeHolder400Change4W: 1.5, TopBrokerNetBuy20D: 100, BrokerPersistenceScore: 70 },
    coverage,
    latest_data: "2026-08-20",
  };
});

function detail(stock: FixtureStock) {
  return {
    stock,
    score: {
      score: stock.score,
      status: stock.status,
      score_version: "s-only-v6",
      formula_hash: formulaHash,
      coverage,
      source_date: "2026-08-20",
      calculated_at: "2026-08-20T13:00:00Z",
      knowledge_cutoff: "2026-08-20T13:00:00Z",
      input_snapshot_hash: "b".repeat(64),
      explanation: [
        { label: "Institutional", value: 80, detail: "persistent institutional flow" },
        { label: "Ownership", value: 75, detail: "ownership accumulation" },
        { label: "Broker", value: 70, detail: "contract-bound broker persistence" },
        { label: "Modifier", value: 2, detail: "supporting price modifier" },
      ],
    },
    sources: {
      institutional: { provider: "FinMind", dataset: "TaiwanStockInstitutionalInvestorsBuySellWide", latest_source_date: "2026-08-20", fetched_at: "2026-08-20T13:00:00Z", row_count: 20, staleness: "FRESH" },
      foreign_holding: { provider: "FinMind", dataset: "TaiwanStockShareholding", latest_source_date: "2026-08-20", fetched_at: "2026-08-20T13:00:00Z", row_count: 21, staleness: "FRESH" },
      holding_distribution: { provider: "FinMind", dataset: "TaiwanStockHoldingSharesPer", latest_source_date: "2026-08-20", fetched_at: "2026-08-20T13:00:00Z", row_count: 75, staleness: "FRESH" },
      broker: { provider: "FinMind", dataset: "TaiwanStockTradingDailyReport", latest_source_date: "2026-08-20", fetched_at: "2026-08-20T13:00:00Z", row_count: 20, staleness: "FRESH" },
      price: { provider: "FinMind", dataset: "TaiwanStockPrice", latest_source_date: "2026-08-20", fetched_at: "2026-08-20T13:00:00Z", row_count: 21, staleness: "FRESH" },
      major_shareholder_5pct: { provider: "TWSE/TPEx/MOPS", dataset: null, status: "UNAVAILABLE_NOT_CONFIGURED", row_count: 0 },
    },
    institutional: [1, 2, 3, 4, 5].map((value) => ({ foreign_net: value, investment_trust_net: value + 1, dealer_net: value - 1 })),
    foreign_holding: [40, 40.2, 40.4, 40.6, 40.8].map((value) => ({ foreign_investment_shares_ratio: value })),
    holding_distribution: [],
    holding_series: {
      "400": [10, 11, null, 13, 14].map((value, index) => ({ source_date: `2026-07-${10 + index}`, value })),
      "1000": [5, 6, null, 7, 8].map((value, index) => ({ source_date: `2026-07-${10 + index}`, value })),
    },
    brokers: [],
    prices: [100, 101, 102, 103, 104].map((close) => ({ close })),
    score_history: [70, 71, null, 73, 74].map((score) => ({ score })),
    calendar_version: "tw-exchange-2026-v1",
  };
}

async function installApiFixtures(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/summary") {
      return route.fulfill({ json: { stock_count: 55, strong_count: 21, accumulation_count: 15, watch_count: 15, data_insufficient_count: 3, no_strong_evidence_count: 1, status_invariant: true, score_version: "s-only-v6", formula_hash: formulaHash, latest_score_date: "2026-08-20", sync_status: [] } });
    }
    if (url.pathname === "/api/stocks") {
      // Force the initial unfiltered request to finish after a rapid filter request.
      // The UI must never allow this stale response to overwrite current results.
      if (!url.searchParams.has("search") && !url.searchParams.has("market") && !url.searchParams.has("status") && !url.searchParams.has("min_score")) {
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      let filtered = [...stocks];
      const search = url.searchParams.get("search")?.toLowerCase();
      if (search) filtered = filtered.filter((stock) => stock.stock_id.includes(search) || stock.stock_name.toLowerCase().includes(search));
      const market = url.searchParams.get("market");
      if (market) filtered = filtered.filter((stock) => stock.market === market);
      const status = url.searchParams.get("status");
      if (status) filtered = filtered.filter((stock) => stock.status === status);
      const minScore = url.searchParams.get("min_score");
      if (minScore) filtered = filtered.filter((stock) => stock.score != null && stock.score >= Number(minScore));
      const sort = url.searchParams.get("sort") || "score";
      filtered.sort((left, right) => {
        if (sort === "score") {
          if (left.score == null) return right.score == null ? left.stock_id.localeCompare(right.stock_id) : 1;
          if (right.score == null) return -1;
          return right.score - left.score || left.stock_id.localeCompare(right.stock_id);
        }
        return String(right[sort as "stock_id" | "stock_name"]).localeCompare(String(left[sort as "stock_id" | "stock_name"]));
      });
      const pageNumber = Number(url.searchParams.get("page") || 1);
      const pageSize = Number(url.searchParams.get("page_size") || 50);
      return route.fulfill({ json: { items: filtered.slice((pageNumber - 1) * pageSize, pageNumber * pageSize), total: filtered.length, page: pageNumber, page_size: pageSize } });
    }
    if (url.pathname === "/api/rankings") {
      const limit = Number(url.searchParams.get("limit") || 50);
      return route.fulfill({ json: { source_date: "2026-08-20", score_version: "s-only-v6", items: stocks.filter((stock) => stock.score != null).slice(0, limit) } });
    }
    if (url.pathname === "/api/score-spec") {
      return route.fulfill({ json: { score_version: "s-only-v6", formula_hash: formulaHash, spec: { weights: { institutional_persistence: 0.35, ownership_accumulation: 0.35, broker_persistence: 0.30 } } } });
    }
    const match = url.pathname.match(/^\/api\/stocks\/(\d+)$/);
    if (match) {
      const stock = stocks.find((item) => item.stock_id === match[1]);
      return stock ? route.fulfill({ json: detail(stock) }) : route.fulfill({ status: 404, json: { detail: "stock not found" } });
    }
    return route.fulfill({ status: 404, json: { detail: "fixture route missing" } });
  });
}

test.beforeEach(async ({ page }) => {
  await installApiFixtures(page);
});

test("summary count invariant and deterministic ranking contract are exact", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("55 檔 · 20D persistence weighted")).toBeVisible();
  await expect(page.getByText("21", { exact: true })).toBeVisible();
  const contract = await page.evaluate(async () => {
    const [summary, rankings, spec] = await Promise.all([
      fetch("/api/summary").then((response) => response.json()),
      fetch("/api/rankings?kind=top&limit=10").then((response) => response.json()),
      fetch("/api/score-spec").then((response) => response.json()),
    ]);
    return { summary, rankings, spec };
  });
  expect(contract.summary.status_invariant).toBe(true);
  expect(contract.summary.strong_count + contract.summary.accumulation_count + contract.summary.watch_count + contract.summary.data_insufficient_count + contract.summary.no_strong_evidence_count).toBe(contract.summary.stock_count);
  expect(contract.rankings.items.map((item: FixtureStock) => item.score)).toEqual([100, 99, 98, 97, 96, 95, 94, 93, 92, 91]);
  expect(contract.spec.formula_hash).toBe(formulaHash);
  expect(contract.spec.spec.weights).toEqual({ institutional_persistence: 0.35, ownership_accumulation: 0.35, broker_persistence: 0.30 });
});

test("market status and minimum-score filters compose through the UI", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("市場").selectOption("上市");
  await page.getByLabel("狀態").selectOption("STRONG_ACCUMULATION");
  await page.getByLabel("Score ≥").fill("90");
  const expectedIds = stocks.filter((stock) => stock.market === "上市" && stock.status === "STRONG_ACCUMULATION" && stock.score != null && stock.score >= 90).map((stock) => stock.stock_id);
  await expect.poll(async () => page.getByTestId("stock-row").count()).toBe(expectedIds.length);
  expect(await page.getByTestId("stock-row").evaluateAll((rows) => rows.map((row) => row.getAttribute("data-stock-id")))).toEqual(expectedIds);
  await expect(page.getByTestId("filtered-total")).toContainText(`${expectedIds.length} 檔`);
});

test("a delayed initial response cannot overwrite newer search results", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("股票代碼或名稱搜尋").fill("1007");
  await expect(page.getByTestId("stock-row")).toHaveCount(1);
  await expect(page.getByTestId("stock-row")).toHaveAttribute("data-stock-id", "1007");
  await page.waitForTimeout(400);
  await expect(page.getByTestId("stock-row")).toHaveCount(1);
  await expect(page.getByTestId("stock-row")).toHaveAttribute("data-stock-id", "1007");
  await expect(page.getByTestId("filtered-total")).toContainText("1 檔");
});

test("score pagination keeps numeric values ahead of null and sorting resets page", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("stock-row")).toHaveCount(50);
  await page.getByRole("button", { name: "下一頁 →" }).click();
  await expect(page.getByText("第 2 頁")).toBeVisible();
  await expect(page.getByTestId("stock-row")).toHaveCount(5);
  expect(await page.locator(".score-cell b").allTextContents()).toEqual(["50.0", "49.0", "—", "—", "—"]);
  await page.getByLabel("排序").selectOption("stock_id");
  await expect(page.getByText("第 1 頁")).toBeVisible();
  await expect(page.getByTestId("stock-row").first()).toHaveAttribute("data-stock-id", "1054");
});

test("detail exposes provenance formula broker caveat 5-percent unavailable and null gaps", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("stock-row").first().click();
  await expect(page.getByText("Final = institutional 35% + ownership 35% + broker 30% + low-profile modifier。", { exact: false })).toBeVisible();
  await expect(page.getByText(`Formula hash ${formulaHash}`)).toBeVisible();
  await expect(page.getByText("v6 只計入逐列驗證的正買超事件，未出現分點保持 unknown，絕不補零。", { exact: false })).toBeVisible();
  await expect(page.getByText("分點資料 unavailable")).toBeVisible();
  await expect(page.getByTestId("source-major_shareholder_5pct")).toContainText("持股超過 5% 股東");
  await expect(page.getByTestId("source-major_shareholder_5pct")).toContainText("UNAVAILABLE_NOT_CONFIGURED");
  const holdingChart = page.getByRole("region", { name: ">400／>1000 lots 持股比例" });
  await expect(holdingChart.locator("polyline")).toHaveCount(4);
});
