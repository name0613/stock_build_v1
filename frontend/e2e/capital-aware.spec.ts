import { expect, Page, test } from "@playwright/test";

const capitalScore = {
  score: 81.2,
  status: "HIGH_CONFIDENCE_ACCUMULATION",
  score_version: "capital-aware-v7",
  formula_hash: "c".repeat(64),
  large_capital_score: 88.5,
  high_confidence_score: 81.2,
  components: { StealthAccumulationScore: 76, LiquidityScore: 84, CapitalScaleScore: 91, ConfirmationScore: 75, ConfirmationSourceCount: 3, eligibility_reasons: [] },
  features: { MedianTradingValue20D: 240000000, EstimatedInstitutionalNetValue20D: 620000000, InstitutionalNetToTradingValue20D: 0.129, PriceReturn20D: 0.04, TradingValueSeries20D: [{ source_date: "2026-08-19", value: 220000000 }, { source_date: "2026-08-20", value: 240000000 }], EstimatedInstitutionalNetValueSeries20D: [{ source_date: "2026-08-19", value: 29000000 }, { source_date: "2026-08-20", value: 41000000 }], LargeHolder400Change4W: 1.1 },
  coverage: { capital_aware: { eligible: true, eligibility_reasons: [] } },
  source_date: "2026-08-20",
  calculated_at: "2026-08-20T13:00:00Z",
  eligibility_reasons: [],
};

async function installCapitalFixtures(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/summary") return route.fulfill({ json: { stock_count: 1, strong_count: 0, accumulation_count: 0, watch_count: 0, data_insufficient_count: 0, score_version: "s-only-v6", formula_hash: "a".repeat(64), latest_score_date: "2026-08-20", sync_status: [], capital_aware_score_version: "capital-aware-v7", capital_aware_formula_hash: capitalScore.formula_hash, capital_ranking_metrics: { large_capital: { scorable: 1, data_insufficient: 0, gate_excluded: 0, eligible: 1 }, high_confidence: { scorable: 1, data_insufficient: 0, gate_excluded: 0, eligible: 1 } } } });
    if (url.pathname === "/api/holdings/status") return route.fulfill({ json: { dataset: "TaiwanStockHoldingSharesPer", market_session_required: false, total: 1, available_count: 1, items: [] } });
    if (url.pathname === "/api/score-spec") return route.fulfill({ json: { score_version: "s-only-v6", formula_hash: "a".repeat(64), capital_aware_score_version: "capital-aware-v7", capital_aware_formula_hash: capitalScore.formula_hash, capital_aware_spec: { missing_policy: "Trading_money missing stays unavailable" } } });
    if (url.pathname === "/api/rankings") {
      const kind = url.searchParams.get("kind") || "high_confidence";
      return route.fulfill({ json: { source_date: "2026-08-20", kind, score_version: "capital-aware-v7", formula_hash: capitalScore.formula_hash, items: [{ stock_id: "2330", stock_name: "Fixture Capital", market: "上市", industry: "半導體", is_favorite: false, score: kind === "large_capital" ? capitalScore.large_capital_score : capitalScore.high_confidence_score, status: capitalScore.status, score_version: "capital-aware-v7", formula_hash: capitalScore.formula_hash, source_date: capitalScore.source_date, stealth_score: 76, liquidity_score: 84, capital_scale_score: 91, confirmation_score: 75, large_capital_score: capitalScore.large_capital_score, high_confidence_score: capitalScore.high_confidence_score, median_trading_value_20d: 240000000, estimated_institutional_net_value_20d: 620000000, institutional_net_to_trading_value_20d: 0.129, confirmation_source_count: 3, price_return_20d: 0.04, eligibility_reasons: [], features: capitalScore.features, coverage: capitalScore.coverage }] } });
    }
    if (url.pathname === "/api/stocks") return route.fulfill({ json: { items: [], total: 0, page: 1, page_size: 50 } });
    if (url.pathname === "/api/stocks/2330") return route.fulfill({ json: { stock: { stock_id: "2330", stock_name: "Fixture Capital", market: "上市", industry: "半導體", is_favorite: false }, score: { score: 74, status: "STRONG_ACCUMULATION", score_version: "s-only-v6", formula_hash: "a".repeat(64), coverage: {}, explanation: [{ label: "Institutional", value: 70, detail: "v6" }] }, capital_aware_score: capitalScore, sources: {}, institutional: [], foreign_holding: [], holding_distribution: [], holding_series: { "400": [], "1000": [] }, brokers: [], prices: [], score_history: [], calendar_version: "tw-exchange-2026-v1" } });
    if (url.pathname === "/api/readiness") return route.fulfill({ json: { stock_id: "2330", ready: true, missing_reasons: [], source_date: "2026-08-20", coverage: {}, capital_aware_score: capitalScore } });
    return route.fulfill({ status: 404, json: { detail: "fixture route missing" } });
  });
}

test.beforeEach(async ({ page }) => { await installCapitalFixtures(page); });

test("defaults to high-confidence and switches the three capital-aware tabs", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("button", { name: "高可信建倉" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("filtered-total")).toContainText("capital-aware-v7");
  await expect(page.getByTestId("stock-row")).toContainText("81.2");
  await expect(page.getByRole("columnheader", { name: /20D 估算法人淨買金額/ })).toBeVisible();
  await expect(page.getByText("新台幣", { exact: false }).first()).toBeVisible();
  await page.getByRole("button", { name: "大型資金建倉" }).click();
  await expect(page.getByRole("heading", { name: "大型資金建倉榜" })).toBeVisible();
  await expect(page.getByTestId("stock-row")).toContainText("88.5");
  await page.getByRole("button", { name: "隱性建倉" }).click();
  await expect(page.getByRole("heading", { name: "隱性建倉榜" })).toBeVisible();
});

test("capital-aware detail exposes score decomposition, gates and money chart", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("stock-row").click();
  await expect(page.getByTestId("capital-aware-detail")).toContainText("真正大資金持續建倉判定");
  await expect(page.getByTestId("capital-aware-detail")).toContainText("估算金額不是實際成交成本");
  await expect(page.getByTestId("capital-aware-detail")).toContainText("確認來源 3 類");
  await expect(page.getByRole("region", { name: "每日成交金額／估算法人淨買金額" })).toBeVisible();
});

test("detail keeps the latest capital snapshot when readiness target has no v7 row", async ({ page }) => {
  await page.route("**/api/readiness**", async route => route.fulfill({ json: {
    stock_id: "2330",
    ready: false,
    missing_reasons: ["missing_price"],
    source_date: "2026-08-21",
    latest_ready_source_date: "2026-08-20",
    fallback_available: true,
    coverage: {},
    capital_aware_score: { status: "DATA_INSUFFICIENT", score_version: "capital-aware-v7", components: {}, coverage: {} },
  } }));
  await page.goto("/");
  await page.getByTestId("stock-row").click();
  const panel = page.getByTestId("capital-aware-detail");
  await expect(panel).toContainText("81.2");
  await expect(panel).toContainText("88.5");
  await expect(panel).toContainText("目前評估日尚未產生 v7 快照");
  await expect(panel).toContainText("資料日 2026-08-20");
});
