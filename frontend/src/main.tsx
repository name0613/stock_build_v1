import { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type ProviderState = { status?: string; reason_code?: string; score_ready?: boolean; score_blocked?: boolean; score_blocking_reason?: string; blocking_sources?: { dataset?: string; reason_code?: string; retryable_pending?: number }[] };
type Summary = { stock_count: number; strong_count: number; accumulation_count: number; watch_count: number; data_insufficient_count: number; no_strong_evidence_count?: number; status_invariant?: boolean; score_version?: string; formula_hash?: string; latest_score_date?: string; last_data_update?: string; score_ready?: boolean; provider_state?: ProviderState; score_metrics?: { universe_stock_count?: number; evaluated_stock_count?: number; ready_stock_count?: number; not_ready_stock_count?: number; score_rows_processed?: number; score_rows_data_insufficient?: number; score_rows_failed?: number; missing_reason_counts?: Record<string, number>; accounting_invariant?: boolean }; sync_status: SyncStatus[] };
type SyncStatus = { dataset: string; status: string; latest_source_date?: string; last_successful_sync?: string; last_fetch_at?: string; records: number; usable_records?: number; stored_records?: number; staleness?: string; error_code?: string; blocking_reason?: string };
type HoldingStatus = { stock_id: string; stock_name: string; market: string; latest_source_date?: string; status: string; large_holder_400_lots_percent?: number; large_holder_400_lots_people?: number; large_holder_1000_lots_percent?: number; large_holder_1000_lots_people?: number };
type HoldingStatusSnapshot = { dataset: string; market_session_required: boolean; total: number; available_count: number; items: HoldingStatus[] };
type WorkerHealth = { heartbeat?: { market_session?: { state?: string } } };
type Stock = { stock_id: string; stock_name: string; market: string; industry?: string; price?: number; price_change?: number; score?: number; status: string; score_version?: string; features: Record<string, any>; coverage: Record<string, any>; latest_data?: string; data_status?: string; data_latest_source_date?: string; last_updated_at?: string; data_sources?: Record<string, { available?: boolean; latest_source_date?: string; last_updated_at?: string; row_count?: number }> };
type Detail = { stock: Stock; provider_state?: ProviderState; score: { score?: number; status: string; score_version?: string; formula_hash?: string; components?: Record<string, number>; explanation?: { label: string; value: number; detail: string }[]; coverage?: Record<string, boolean>; source_date?: string; calculated_at?: string; knowledge_cutoff?: string; input_snapshot_hash?: string }; sources: Record<string, { provider?: string; dataset?: string; status?: string; latest_source_date?: string; fetched_at?: string; last_successful_fetch?: string; row_count?: number; staleness?: string }>; institutional: any[]; foreign_holding: any[]; holding_distribution: any[]; holding_series: Record<string, { source_date: string; value: number | null }[]>; brokers: any[]; prices: any[]; score_history: any[] };
type ScoreJob = { job_id: number; status: string; run_mode?: string; target_date?: string; started_at?: string; finished_at?: string; processed_stock_count?: number; universe_stock_count?: number; scores?: Record<string, number>; score_metrics?: Summary["score_metrics"]; error_code?: string };
type TargetedScoreJob = { job_id: number; stock_id?: string; status: string; run_mode?: string; target_date?: string; evaluated_source_date?: string; fallback_applied?: boolean; fallback_reason?: string; phase?: string; progress?: { completed?: number; total?: number }; started_at?: string; finished_at?: string; datasets?: Record<string, { status?: string; error_code?: string; physical_requests?: number; records_accepted?: number }>; pre_readiness?: Readiness; readiness?: Readiness; score?: { score?: number; status?: string; source_date?: string }; fetch_errors?: { dataset?: string; error_code?: string }[]; quota?: { status?: string; remaining?: number; error_code?: string }; error_code?: string };
type Readiness = { stock_id: string; ready: boolean; missing_reasons: string[]; latest_ready_source_date?: string | null; fallback_available?: boolean; coverage: { readiness_reason_codes?: string[]; missing_sessions?: Record<string, string[]>; holding_missing_weeks?: number[]; RequiredFeatureValidation?: Record<string, { valid?: boolean; reason?: string; expected_window?: number; cadence?: string }> }; source_date: string; knowledge_cutoff?: string };
type ChartPoint = { value: number | null; label?: string };
type ChartSeries = { name: string; color: string; values: ChartPoint[]; axis?: "left" | "right" };
type ChartAxis = { leftLabel: string; rightLabel?: string; format: "integer" | "price" | "percent" | "score" };

const statusLabel: Record<string, string> = { STRONG_ACCUMULATION: "Strong Accumulation", ACCUMULATION: "Accumulation", WATCH: "Watch", NO_STRONG_EVIDENCE: "No Strong Evidence", DATA_INSUFFICIENT: "Data Insufficient", SCORE_BLOCKED_BY_SOURCE_COVERAGE: "Score blocked by source coverage" };
const reasonLabel: Record<string, string> = { WAITING_FOR_PROVIDER_PUBLICATION: "等待 FinMind 每週持股資料發布", HOLDING_PUBLICATION_PARTIAL: "每週持股資料尚未完整", HOLDING_BUCKETS_INCOMPLETE: "持股級距未完整（需要 15 個標準級距）", BROKER_RETRY_PENDING: "分點 catch-up 尚有待重試項目", QUOTA_EXHAUSTED: "已達配額保留線，等待下一個 quota window", SOURCE_DATE_STALE: "必要來源日期過舊", STALE_PROVIDER_COVERAGE: "必要來源尚未達到最新資料日", SCORE_BLOCKED_BY_SOURCE_COVERAGE: "Score 已在來源覆蓋率閘門停止" };
const stockReasonLabel: Record<string, string> = { missing_institutional: "三大法人：20 個交易日資料不足", missing_foreign_holding: "外資持股：21 個交易日資料不足", tdcc_required_buckets_incomplete: "集保持股：4 週或標準級距資料不足", missing_broker: "分點：20 個交易日資料不足或列資料未驗證", missing_price: "股價：21 個交易日資料不足", score_evaluation_failed: "評分計算發生例外，已安全標示資料不足" };
const featureLabel: Record<string, string> = { InstitutionalNet20D: "法人 20D 淨買超", InstitutionalPositiveDayRatio20D: "法人正買超日比例", InstitutionalNetSlope20D: "法人淨買斜率", InstitutionalOneDaySpikeRatio20D: "法人單日尖峰比例", ForeignShareRatioChange20D: "外資持股比例 20D 變化", LargeHolder400Change4W: ">400 張持股 4W 變化", BrokerPersistenceScore: "分點持續性分數", PriceReturn20D: "股價 20D 報酬" };
const datasetLabels: Record<string, string> = { institutional: "三大法人", foreign_holding: "外資持股", holding_distribution: "集保持股", broker: "分點", price: "股價／成交量", major_shareholder_5pct: "持股超過 5% 股東" };
const MARKET_REFRESH_INTERVAL_MS = 30 * 60 * 1000;
const chartAxisMetadata: Record<string, { x: string; y: string; note?: string }> = {
  "股價／Accumulation Score": { x: "來源日期（由左至右：舊 → 新）", y: "左：價格（元）／右：Score（0–100 分）", note: "Price 與 Score 使用左右兩條 Y 軸，請分別看各自趨勢，不直接比較線條高度。" },
  "法人每日 Net Buy": { x: "交易日（由左至右：舊 → 新）", y: "法人淨買超（股）" },
  "外資實際持股比例": { x: "資料日期（由左至右：舊 → 新）", y: "外資持股比例（%）" },
  ">400／>1000 lots 持股比例": { x: "週資料日期（由左至右：舊 → 新）", y: "持股比例（%）" },
};

function App() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [items, setItems] = useState<Stock[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [market, setMarket] = useState("");
  const [status, setStatus] = useState("");
  const [minScore, setMinScore] = useState("");
  const [sort, setSort] = useState("score");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [holdingStatus, setHoldingStatus] = useState<HoldingStatusSnapshot | null>(null);
  const [holdingStatusError, setHoldingStatusError] = useState(false);
  const [scoreJob, setScoreJob] = useState<ScoreJob | null>(null);
  const [scoreActionError, setScoreActionError] = useState<string | null>(null);
  const stocksRequestId = useRef(0);
  const holdingStatusRequest = useRef<Promise<void>>(Promise.resolve());
  const refreshInFlight = useRef(false);

  async function loadSummary() { try { setSummary(await fetchJson<Summary>("/api/summary", undefined, { cache: "no-store" })); } catch { setError("API 尚未可用，請確認服務與資料庫狀態。"); } }
  async function loadHoldingStatus() {
    try {
      setHoldingStatus(await fetchJson<HoldingStatusSnapshot>("/api/holdings/status", undefined, { cache: "no-store" }));
      setHoldingStatusError(false);
    } catch {
      setHoldingStatusError(true);
    }
  }
  async function loadStocks(signal: AbortSignal, requestId: number) {
    setLoading(true);
    try {
      if (signal.aborted) return;
      const params = new URLSearchParams({ page: String(page), page_size: "50", sort, order: "desc" });
      if (search) params.set("search", search); if (market) params.set("market", market); if (status) params.set("status", status); if (minScore) params.set("min_score", minScore);
      const response = await fetchJson<{ items: Stock[]; total: number }>(`/api/stocks?${params}`, signal, { cache: "no-store" });
      if (requestId !== stocksRequestId.current) return;
      setItems(response.items); setTotal(response.total); setError(null);
    } catch (error) {
      if (signal.aborted || requestId !== stocksRequestId.current) return;
      setError("無法讀取股票清單；資料不足時系統會維持 DATA_INSUFFICIENT，不會補成 0。");
    } finally {
      if (requestId === stocksRequestId.current) setLoading(false);
    }
  }
  async function loadDetail(stockId: string): Promise<void> {
    try {
      setDetail(await fetchJson<Detail>(`/api/stocks/${stockId}`, undefined, { cache: "no-store" }));
    } catch {
      setError("個股資料讀取失敗。");
    }
  }
  async function loadScoreJob(jobId?: number) {
    try {
      const query = jobId ? `?job_id=${jobId}` : "";
      setScoreJob(await fetchJson<ScoreJob>(`/api/score/current${query}`, undefined, { cache: "no-store" }));
    } catch {
      // No score job exists on a fresh installation; keep the controls quiet.
    }
  }
  async function refreshSnapshot() {
    await loadSummary();
    const controller = new AbortController();
    const requestId = ++stocksRequestId.current;
    await loadStocks(controller.signal, requestId);
    if (selected) await loadDetail(selected);
  }
  async function startCurrentScore() {
    if (scoreJob?.status === "RUNNING") return;
    setScoreActionError(null);
    try {
      const started = await fetchJson<ScoreJob>("/api/score/current", undefined, { method: "POST" });
      setScoreJob(started);
    } catch {
      setScoreActionError("評分作業無法啟動；請確認是否已有評分作業執行中。");
    }
  }
  async function refreshOpenMarketData() {
    if (refreshInFlight.current) return;
    refreshInFlight.current = true;
    try {
      const health = await fetchJson<WorkerHealth>("/api/worker-health", undefined, { cache: "no-store" });
      if (health.heartbeat?.market_session?.state !== "OPEN") return;
      holdingStatusRequest.current = loadHoldingStatus();
      await holdingStatusRequest.current;
      await loadSummary();
      const controller = new AbortController();
      const requestId = ++stocksRequestId.current;
      await loadStocks(controller.signal, requestId);
      if (selected) await loadDetail(selected);
    } catch {
      // Keep the last rendered snapshot when the worker health endpoint is unavailable.
    } finally {
      refreshInFlight.current = false;
    }
  }
  useEffect(() => { holdingStatusRequest.current = loadHoldingStatus(); }, []);
  useEffect(() => { void loadSummary(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    const requestId = ++stocksRequestId.current;
    void loadStocks(controller.signal, requestId);
    return () => controller.abort();
  }, [page, search, market, status, minScore, sort]);
  useEffect(() => { if (!selected) return; void loadDetail(selected); }, [selected]);
  useEffect(() => { void loadScoreJob(); }, []);
  useEffect(() => {
    if (!scoreJob || scoreJob.status !== "RUNNING") return;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const next = await fetchJson<ScoreJob>(`/api/score/current?job_id=${scoreJob.job_id}`, undefined, { cache: "no-store" });
          setScoreJob(next);
          if (next.status !== "RUNNING") await refreshSnapshot();
        } catch {
          setScoreActionError("評分進度讀取失敗，請稍後重新整理。");
        }
      })();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [scoreJob?.job_id, scoreJob?.status]);
  useEffect(() => {
    const interval = window.setInterval(() => { void refreshOpenMarketData(); }, MARKET_REFRESH_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [page, search, market, status, minScore, sort, selected]);

  const filtered = useMemo(() => items, [items]);
  if (detail && selected) return <DetailPage detail={detail} onBack={() => { setSelected(null); setDetail(null); }} onRefresh={() => loadDetail(selected)} />;
  return <div className="app-shell">
    <header className="topbar"><div><p className="eyebrow">TAIWAN STOCK MARKET · S-ONLY EVIDENCE</p><h1>低調持續建倉監控</h1><p className="subtitle">把連續、分散、可追溯的籌碼集中證據放在同一張桌上。</p></div><div className="header-meta"><span className="live-dot" /> Asia/Taipei<br /><small>Score version {summary?.score_version || "—"}</small></div></header>
    <main>
       <div className="notice"><strong>Evidence only</strong>　這裡顯示法人／大型資金持續性證據，不代表單一投資人、主力、買進建議或報酬保證。缺資料會顯示 DATA_INSUFFICIENT。</div>
       {summary?.provider_state?.score_blocked && <div className="error-banner" data-testid="score-blocking-reason"><strong>全市場來源同步尚未完整</strong>　已具備可追溯資料的個股 Score 仍會顯示；尚未通過個股資料合約者維持 Data Insufficient。</div>}
      <section className="summary-grid"><Metric title="股票總數" value={summary?.stock_count ?? "—"} accent="blue" /><Metric title="Strong Accumulation" value={summary?.strong_count ?? "—"} accent="green" /><Metric title="Accumulation" value={summary?.accumulation_count ?? "—"} accent="amber" /><Metric title="Watch" value={summary?.watch_count ?? "—"} accent="purple" /><Metric title="Data Insufficient" value={summary?.data_insufficient_count ?? "—"} accent="red" /></section>
      <section className="panel controls"><div className="control-title"><div><span className="eyebrow">UNIVERSE VIEW</span><h2>全部普通股</h2></div><span className="data-date">最新評分日 {summary?.latest_score_date ?? "尚無資料"}<br /><small data-testid="holding-status-load">大戶持有狀態 {holdingStatus ? `${holdingStatus.available_count.toLocaleString()} / ${holdingStatus.total.toLocaleString()} 檔已載入` : holdingStatusError ? "讀取失敗" : "載入中…"}</small><br /><small data-testid="score-readiness-metrics">逐檔評分 {summary?.score_metrics ? `${summary.score_metrics.ready_stock_count ?? 0} ready / ${summary.score_metrics.not_ready_stock_count ?? 0} not ready · ${summary.score_metrics.score_rows_processed ?? 0} 筆 numeric` : "尚無執行紀錄"}</small></span></div><div className="control-row"><label>搜尋<input aria-label="股票代碼或名稱搜尋" value={search} onChange={e => { setPage(1); setSearch(e.target.value); }} placeholder="代碼或名稱，例如 2330" /></label><label>市場<select value={market} onChange={e => { setPage(1); setMarket(e.target.value); }}><option value="">全部</option><option value="上市">上市</option><option value="上櫃">上櫃</option><option value="興櫃">興櫃</option></select></label><label>狀態<select value={status} onChange={e => { setPage(1); setStatus(e.target.value); }}><option value="">全部狀態</option>{Object.entries(statusLabel).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label>Score ≥<input type="number" min="0" max="100" value={minScore} onChange={e => { setPage(1); setMinScore(e.target.value); }} placeholder="—" /></label><label>排序<select value={sort} onChange={e => { setPage(1); setSort(e.target.value); }}><option value="score">Score</option><option value="stock_id">代碼</option><option value="stock_name">名稱</option></select></label><button className="ghost-button" onClick={() => { setStatus("STRONG_ACCUMULATION"); setPage(1); }}>只看 Strong</button><button className="primary-button" data-testid="score-current-button" disabled={scoreJob?.status === "RUNNING"} onClick={() => void startCurrentScore()}>{scoreJob?.status === "RUNNING" ? `評分執行中 ${scoreJob.processed_stock_count ?? 0}/${scoreJob.universe_stock_count ?? "…"}` : "依目前資料評分"}</button></div>{scoreActionError && <p className="action-error">{scoreActionError}</p>}{scoreJob && scoreJob.status !== "RUNNING" && <p className="action-status" data-testid="score-current-status">最近一次本機評分：{scoreJob.status} · {scoreJob.target_date || "—"} · {scoreJob.score_metrics?.score_rows_processed ?? 0} 筆 numeric</p>}</section>
      {error && <div className="error-banner">{error}</div>}
        <section className="panel table-panel"><div className="table-head"><div><span className="eyebrow">ACCUMULATION RANKING</span><h2>證據總覽</h2></div><span data-testid="filtered-total">{total.toLocaleString()} 檔 · 20D persistence weighted</span></div><p className="partial-data-note">各股票採「已寫入即可顯示」；只要該股票已有可追溯的 numeric Score 就會顯示，缺少必要資料者維持 DATA_INSUFFICIENT。資料日是來源日期，寫入時間是本機實際取得時間。</p><div className="table-scroll"><table><thead><tr><th>股票</th><th>市場／產業</th><th>最新價</th><th>Score / 狀態</th><th>外資 5D / 20D</th><th>投信 5D / 20D</th><th>外資持股比例<br />20D change</th><th>&gt;400 張<br />4W change</th><th>Top Broker 20D</th><th>Coverage / 最新資料日／寫入時間</th></tr></thead><tbody>{loading ? <tr><td colSpan={10} className="empty">資料讀取中…</td></tr> : filtered.length === 0 ? <tr><td colSpan={10} className="empty">尚無可呈現資料。請先完成 FinMind sync；系統不會以 0 偽造缺失資料。</td></tr> : filtered.map(item => <tr key={item.stock_id} data-testid="stock-row" data-stock-id={item.stock_id} onClick={() => setSelected(item.stock_id)} className="clickable"><td><strong>{item.stock_id}</strong><br /><span>{item.stock_name}</span></td><td>{item.market}<br /><small>{item.industry || "—"}</small></td><td>{formatNumber(item.price)}<br /><span className={(item.price_change ?? 0) >= 0 ? "positive" : "negative"}>{formatNumber(item.price_change)}</span></td><td><div className="score-cell"><b className={scoreClass(item.status)}>{item.score == null ? "—" : item.score.toFixed(1)}</b><span>{statusLabel[item.status] || item.status}</span></div></td><td>{formatNumber(item.features.ForeignNet5D)}<br />{formatNumber(item.features.ForeignNet20D)}</td><td>{formatNumber(item.features.InvestmentTrustNet5D)}<br />{formatNumber(item.features.InvestmentTrustNet20D)}</td><td>{formatNumber(item.features.ForeignShareRatioChange20D)}</td><td>{formatNumber(item.features.LargeHolder400Change4W)}</td><td>{formatNumber(item.features.TopBrokerNetBuy20D)}<br /><small>Persistence {formatNumber(item.features.BrokerPersistenceScore)}</small></td><td><Coverage coverage={item.coverage} /><br /><small>資料日 {item.data_latest_source_date || item.latest_data || "尚無"}<br />寫入 {formatDate(item.last_updated_at)}<br />{item.data_status === "PARTIAL" ? "部分來源已寫入" : item.data_status === "COMPLETE" ? "五項來源皆有資料" : "尚無來源資料"}</small></td></tr>)}</tbody></table></div><div className="pagination"><button disabled={page <= 1} onClick={() => setPage(p => p - 1)}>← 上一頁</button><span>第 {page} 頁</span><button disabled={page * 50 >= total} onClick={() => setPage(p => p + 1)}>下一頁 →</button></div></section>
       <section className="panel source-panel"><div><span className="eyebrow">DATA FRESHNESS</span><h2>資料來源健康度</h2></div><div className="source-grid">{(summary?.sync_status || []).map(source => <div className="source-card" key={source.dataset}><span className={source.status === "SUCCESS" || source.status === "REUSED" ? "status-dot ok" : "status-dot warn"} /> <div><strong>{source.dataset}</strong><small>Source date {source.latest_source_date || "unavailable"} · usable {source.usable_records ?? "—"} / stored {source.stored_records ?? "—"}<br />{reasonLabel[source.blocking_reason || ""] || source.blocking_reason || source.staleness || source.status} · fetched {formatDate(source.last_fetch_at)}</small></div></div>)}</div></section>
    </main><footer>Accumulation Evidence · source dates are distinct from fetched_at · S-level datasets only · {summary?.last_data_update ? `last successful sync ${summary.last_data_update}` : "waiting for sync"}</footer>
  </div>;
}

function DetailPage({ detail, onBack, onRefresh }: { detail: Detail; onBack: () => void; onRefresh: () => Promise<void> }) {
  const score = detail.score;
  const holding400 = (detail.holding_series?.["400"] || []).map(point => ({ value: point.value, label: point.source_date }));
  const holding1000 = (detail.holding_series?.["1000"] || []).map(point => ({ value: point.value, label: point.source_date }));
  const [diagnosis, setDiagnosis] = useState<Readiness | null>(null);
  const [diagnosisLoading, setDiagnosisLoading] = useState(true);
  const [diagnosisError, setDiagnosisError] = useState(false);
  const [targetedJob, setTargetedJob] = useState<TargetedScoreJob | null>(null);
  const [targetedActionError, setTargetedActionError] = useState<string | null>(null);
  async function loadDiagnosis() {
    setDiagnosisLoading(true);
    setDiagnosisError(false);
    try {
      setDiagnosis(await fetchJson<Readiness>(`/api/readiness?stock_id=${encodeURIComponent(detail.stock.stock_id)}`, undefined, { cache: "no-store" }));
    } catch {
      setDiagnosisError(true);
    } finally {
      setDiagnosisLoading(false);
    }
  }
  async function startTargetedFetchAndScore() {
    if (targetedJob?.status === "RUNNING") return;
    setTargetedActionError(null);
    try {
      const started = await fetchJson<TargetedScoreJob>(`/api/stocks/${encodeURIComponent(detail.stock.stock_id)}/fetch-and-score`, undefined, { method: "POST" });
      setTargetedJob(started);
    } catch {
      setTargetedActionError("單股補抓無法啟動；請確認是否已有其他單股作業執行中。");
    }
  }
  useEffect(() => { void loadDiagnosis(); }, [detail.stock.stock_id]);
  useEffect(() => {
    if (!targetedJob || targetedJob.status !== "RUNNING") return;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const next = await fetchJson<TargetedScoreJob>(`/api/stocks/${encodeURIComponent(detail.stock.stock_id)}/fetch-and-score?job_id=${targetedJob.job_id}`, undefined, { cache: "no-store" });
          setTargetedJob(next);
          if (next.status !== "RUNNING") {
            await onRefresh();
            await loadDiagnosis();
          }
        } catch {
          setTargetedActionError("單股補抓進度讀取失敗，請稍後重新整理。");
        }
      })();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [targetedJob?.job_id, targetedJob?.status, detail.stock.stock_id]);
      return <div className="app-shell"><header className="topbar"><button className="back-button" onClick={onBack}>← 回到全部股票</button><div><p className="eyebrow">STOCK DETAIL · {detail.stock.stock_id}</p><h1>{detail.stock.stock_name} <span className="muted">{detail.stock.stock_id}</span></h1><p className="subtitle">{detail.stock.market} · {detail.stock.industry || "產業未提供"}</p></div><div className="header-meta">Score version<br /><strong>{score.score_version || "—"}</strong></div></header><main><div className="notice"><strong>Why this score</strong>　Final = institutional 35% + ownership 35% + broker 30% + low-profile modifier。分點是券商營業據點的彙總，不等同於一位自然人或「主力」；價格與成交量只作低調 modifier。</div><section className="detail-hero"><div><span className="eyebrow">ACCUMULATION EVIDENCE</span><div className="hero-score">{score.score == null ? "—" : score.score.toFixed(1)}</div><span className={`pill ${scoreClass(score.status)}`}>{statusLabel[score.status] || score.status}</span></div><div className="coverage-box"><h3>Data coverage</h3><Coverage coverage={score.coverage || {}} large /><p>Source date {score.source_date || "unavailable"}<br />Calculated at {formatDate(score.calculated_at)}<br />Input snapshot {score.input_snapshot_hash || "unavailable"}<br />Formula hash {score.formula_hash || "unavailable"}</p></div></section><section className="panel targeted-panel" data-testid="targeted-score-panel"><div className="table-head"><div><span className="eyebrow">TARGETED REMEDIATION</span><h2>單股補抓缺失資料並評分</h2></div><button className="primary-button" data-testid="targeted-fetch-score-button" disabled={targetedJob?.status === "RUNNING"} onClick={() => void startTargetedFetchAndScore()}>{targetedJob?.status === "RUNNING" ? `補抓中 ${targetedJob.progress?.completed ?? 0}/${targetedJob.progress?.total ?? 5}` : "補抓缺失資料後立即評分"}</button></div><p>先補抓最新資料日；若來源尚未發布，會以該股票最近一個資料完整的交易日評分，不以 0 補值。</p>{targetedJob?.status === "RUNNING" && <p className="action-status" data-testid="targeted-score-progress">目前階段：{targetedJob.phase || "排隊中"}</p>}{targetedActionError && <p className="action-error">{targetedActionError}</p>}{targetedJob && targetedJob.status !== "RUNNING" && <p className={targetedJob.status === "SUCCESS" ? "diagnosis-ok" : "diagnosis-summary"} data-testid="targeted-score-status">最近一次結果：{targetedJob.status} · {targetedJob.score?.score == null ? "仍為資料不足" : `Score ${targetedJob.score.score.toFixed(1)}${targetedJob.fallback_applied ? `（資料日 ${targetedJob.evaluated_source_date || targetedJob.score.source_date}，採最近完整資料）` : ""}`} · {targetedJob.readiness?.missing_reasons?.length ? `尚缺 ${targetedJob.readiness.missing_reasons.length} 類資料` : "資料完整"}</p>}</section><ReadinessDiagnosis diagnosis={diagnosis} loading={diagnosisLoading} error={diagnosisError} onRefresh={() => void loadDiagnosis()} /><section className="explanation-grid">{(score.explanation || []).map((part, i) => <div className="explain-card" key={part.label}><span className={`explain-index i${i}`}>0{i + 1}</span><div><strong>{part.label}</strong><b>{part.value.toFixed(1)}</b><p>{part.detail}</p></div></div>)}</section><section className="charts-grid"><ChartCard title="股價／Accumulation Score"><LineChart series={[{ name: "Price", color: "#72a7ff", values: chartValues(detail.prices, "close") }, { name: "Score", color: "#f0b35b", values: chartValues(detail.score_history, "score") }]} /></ChartCard><ChartCard title="法人每日 Net Buy"><LineChart series={[{ name: "外資", color: "#70d6a1", values: chartValues(detail.institutional, "foreign_net") }, { name: "投信", color: "#f597a5", values: chartValues(detail.institutional, "investment_trust_net") }, { name: "自營商", color: "#b7a0f5", values: chartValues(detail.institutional, "dealer_net") }]} /></ChartCard><ChartCard title="外資實際持股比例"><LineChart series={[{ name: "ForeignInvestmentSharesRatio", color: "#70d6a1", values: chartValues(detail.foreign_holding, "foreign_investment_shares_ratio") }]} /></ChartCard><ChartCard title=">400／>1000 lots 持股比例"><LineChart series={[{ name: ">400 lots", color: "#f0b35b", values: holding400 }, { name: ">1000 lots", color: "#a78bfa", values: holding1000 }]} /></ChartCard></section><section className="panel broker-detail"><div className="table-head"><div><span className="eyebrow">BROKER PERSISTENCE</span><h2>Top 20D 分點彙總</h2></div><span>持續承接證據，不指向單一受益所有人；v6 只計入逐列驗證的正買超事件，未出現分點保持 unknown，絕不補零。</span></div><p className="broker-unit-note">買進、賣出、淨買皆為股數（股），不是張數；1 張 = 1,000 股。正值天數與負值天數以交易日計算。</p><div className="table-scroll"><table><thead><tr><th>券商分點</th><th>買進（股）</th><th>賣出（股）</th><th>淨買（股）</th><th>正值天數（交易日）</th><th>負值天數（交易日）</th></tr></thead><tbody>{detail.brokers.length ? detail.brokers.map(b => <tr key={b.securities_trader_id}><td>{b.securities_trader_name || b.securities_trader_id}</td><td>{formatNumber(b.buy_volume)}</td><td>{formatNumber(b.sell_volume)}</td><td className={b.net_volume >= 0 ? "positive" : "negative"}>{formatNumber(b.net_volume)}</td><td>{b.positive_days}</td><td>{b.negative_days}</td></tr>) : <tr><td colSpan={6} className="empty">分點資料 unavailable</td></tr>}</tbody></table></div></section><section className="panel source-panel"><div><span className="eyebrow">PROVENANCE</span><h2>來源與更新時間</h2></div><div className="source-grid">{Object.entries(detail.sources).map(([key, source]) => <div className="source-card" data-testid={`source-${key}`} key={key}><span className={source.status === "UNAVAILABLE_NOT_CONFIGURED" ? "status-dot warn" : "status-dot ok"} /><div><strong>{datasetLabels[key] || key}</strong><small>{source.provider || "—"} · {source.dataset || "not configured"}<br />Source date {source.latest_source_date || "unavailable"} · {source.row_count ?? "—"} rows<br />Fetched {formatDate(source.fetched_at)} · {source.staleness || source.status || "unavailable"}</small></div></div>)}</div></section></main><footer>此頁只呈現可追溯的 accumulation evidence，不提供投資建議。</footer></div>;
}

function ReadinessDiagnosis({ diagnosis, loading, error, onRefresh }: { diagnosis: Readiness | null; loading: boolean; error: boolean; onRefresh: () => void }) {
  const validation = Object.entries(diagnosis?.coverage.RequiredFeatureValidation || {}).filter(([, item]) => item.valid !== true);
  const missingSessions = Object.entries(diagnosis?.coverage.missing_sessions || {}).filter(([, dates]) => dates.length > 0);
  return <section className="panel diagnosis-panel" data-testid="stock-diagnosis"><div className="table-head"><div><span className="eyebrow">READINESS AUDIT</span><h2>為什麼沒有評分</h2></div><button className="ghost-button" onClick={onRefresh} disabled={loading}>{loading ? "檢查中…" : "重新檢查"}</button></div>{loading ? <p className="empty">正在比對目前已寫入資料與評分需求…</p> : error ? <p className="action-error">缺資料診斷讀取失敗，請稍後重試。</p> : diagnosis && <><p className={diagnosis.ready ? "diagnosis-ok" : "diagnosis-summary"}>評估資料日 {diagnosis.source_date}：{diagnosis.ready ? "資料完整，可進行評分" : `尚不能評分，共 ${diagnosis.missing_reasons.length} 類資料不足`}</p>{!diagnosis.ready && diagnosis.latest_ready_source_date && <p className="diagnosis-ok">目前目標日尚未完整；最近完整資料日為 {diagnosis.latest_ready_source_date}，單股補抓會以該日評分。</p>}{!diagnosis.ready && <><div className="reason-list">{diagnosis.missing_reasons.map(reason => <span className="reason-chip" key={reason}>{stockReasonLabel[reason] || reason}</span>)}</div>{validation.length > 0 && <div className="diagnosis-block"><h3>缺少或無效的評分欄位</h3><ul>{validation.map(([key, item]) => <li key={key}><strong>{featureLabel[key] || key}</strong>：{item.reason || "資料不足"}（需要 {item.expected_window ?? "—"} 筆，{item.cadence || "—"}）</li>)}</ul></div>}{missingSessions.length > 0 && <div className="diagnosis-block"><h3>缺少的來源日期</h3><ul>{missingSessions.map(([source, dates]) => <li key={source}><strong>{datasetLabels[source] || source}</strong>：{dates.join("、")}</li>)}</ul></div>}{(diagnosis.coverage.holding_missing_weeks || []).length > 0 && <p className="diagnosis-note">集保持股尚缺週期：{diagnosis.coverage.holding_missing_weeks?.join("、")}。</p>}</>}</>}</section>;
}

function chartValues(rows: any[], field: string): ChartPoint[] { return rows.map(row => ({ value: row[field] == null ? null : Number.isFinite(Number(row[field])) ? Number(row[field]) : null, label: row.source_date || row.date })); }

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  const axes = chartAxisMetadata[title] || { x: "資料序列（由左至右：舊 → 新）", y: "數值（依圖例）" };
  return <section className="panel chart-card" aria-label={title}>
    <h3>{title}</h3>
    {children}
    <div className="chart-axis-info" aria-label={`${title} 座標軸說明`}>
      <span data-testid="chart-axis-x"><b>X 軸</b>{axes.x}</span>
      <span data-testid="chart-axis-y"><b>Y 軸</b>{axes.y}</span>
    </div>
    {axes.note && <p className="chart-axis-note">{axes.note}</p>}
  </section>;
}
function LineChart({ series }: { series: ChartSeries[] }) {
  const axis = chartAxisFor(series);
  const resolvedSeries = series.map(item => ({ ...item, axis: item.axis || (axis.rightLabel && item.name === "Score" ? "right" : "left") as "left" | "right" }));
  const leftValues = resolvedSeries.flatMap(item => item.axis === "left" ? item.values.map(point => point.value) : []).filter((value): value is number => value != null && Number.isFinite(value));
  const rightValues = resolvedSeries.flatMap(item => item.axis === "right" ? item.values.map(point => point.value) : []).filter((value): value is number => value != null && Number.isFinite(value));
  if (!leftValues.length && !rightValues.length) return <div className="chart-empty">資料不足，無法繪製</div>;
  const width = 640;
  const height = 260;
  const leftDomain = chartDomain(leftValues);
  const rightDomain = axis.rightLabel === "Score（分）" ? { min: 0, max: 100 } : rightValues.length ? chartDomain(rightValues) : null;
  const plot = { left: 112, right: width - (rightDomain ? 90 : 22), top: 18, bottom: height - 58 };
  const leftTicks = chartTicks(leftDomain);
  const rightTicks = rightDomain ? chartTicks(rightDomain) : [];
  const timeline = [...(series.reduce((longest, item) => item.values.length > longest.length ? item.values : longest, [] as ChartPoint[]))];
  const xTickIndexes = chartTickIndexes(timeline.length);
  const axisDescription = rightDomain ? `左軸 ${axis.leftLabel}、右軸 ${axis.rightLabel}` : axis.leftLabel;
  return <div>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`資料趨勢圖；X 軸為日期；Y 軸為${axisDescription}`} className="line-chart">
      {leftTicks.map((value, index) => {
        const y = chartY(value, leftDomain, plot);
        return <g key={`left-tick-${index}`}>
          <line x1={plot.left} x2={plot.right} y1={y} y2={y} stroke="#263b64" strokeWidth="1" />
          <text data-testid="chart-y-tick" x={plot.left - 9} y={y + 4} textAnchor="end" fill="#8fa3c8" fontSize="11">{formatAxisTick(value, axis.format)}</text>
        </g>;
      })}
      {rightDomain && rightTicks.map((value, index) => {
        const y = chartY(value, rightDomain, plot);
        return <text data-testid="chart-y-tick" key={`right-tick-${index}`} x={plot.right + 9} y={y + 4} textAnchor="start" fill="#efb45f" fontSize="11">{formatAxisTick(value, "score")}</text>;
      })}
      <line x1={plot.left} x2={plot.left} y1={plot.top} y2={plot.bottom} stroke="#5574a9" strokeWidth="1.2" />
      <line x1={plot.right} x2={plot.right} y1={plot.top} y2={plot.bottom} stroke={rightDomain ? "#9b6e35" : "#5574a9"} strokeWidth="1.2" />
      <line x1={plot.left} x2={plot.right} y1={plot.bottom} y2={plot.bottom} stroke="#5574a9" strokeWidth="1.2" />
      {xTickIndexes.map(index => {
        const x = chartX(index, Math.max(timeline.length, 1), plot);
        return <g key={`x-tick-${index}`}>
          <line x1={x} x2={x} y1={plot.bottom} y2={plot.bottom + 5} stroke="#5574a9" strokeWidth="1" />
          <text data-testid="chart-x-tick" x={x} y={plot.bottom + 20} textAnchor="middle" fill="#8fa3c8" fontSize="11">{timeline[index]?.label ? formatChartDate(timeline[index].label) : `資料點 ${index + 1}`}</text>
        </g>;
      })}
      <text x={(plot.left + plot.right) / 2} y={height - 7} textAnchor="middle" fill="#a9bde1" fontSize="11">X 軸：日期</text>
      <text x="22" y={(plot.top + plot.bottom) / 2} textAnchor="middle" fill="#a9bde1" fontSize="11" transform={`rotate(-90 22 ${(plot.top + plot.bottom) / 2})`}>Y 軸：{axis.leftLabel}</text>
      {rightDomain && <text x={width - 22} y={(plot.top + plot.bottom) / 2} textAnchor="middle" fill="#efb45f" fontSize="11" transform={`rotate(90 ${width - 22} ${(plot.top + plot.bottom) / 2})`}>Y 軸：{axis.rightLabel}</text>}
      {resolvedSeries.flatMap(item => chartSegments(item, item.axis, leftDomain, rightDomain, plot, timeline.length).map((points, index) => <polyline key={`${item.name}-${index}`} fill="none" stroke={item.color} strokeWidth="2.5" points={points} />))}
    </svg>
    <div className="legend">{series.map(item => <span key={item.name}><i style={{ background: item.color }} />{item.name}</span>)}</div>
  </div>;
}

function chartAxisFor(series: ChartSeries[]): ChartAxis {
  const names = new Set(series.map(item => item.name));
  if (names.has("Price") && names.has("Score")) return { leftLabel: "價格（元）", rightLabel: "Score（分）", format: "price" };
  if (names.has("ForeignInvestmentSharesRatio") || names.has(">400 lots") || names.has(">1000 lots")) return { leftLabel: names.has("ForeignInvestmentSharesRatio") ? "外資持股比例（%）" : "持股比例（%）", format: "percent" };
  return { leftLabel: "法人淨買超（股）", format: "integer" };
}

function chartDomain(values: number[]): { min: number; max: number } {
  if (!values.length) return { min: 0, max: 1 };
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const padding = minValue === maxValue ? Math.max(Math.abs(minValue) * 0.1, 1) : (maxValue - minValue) * 0.08;
  return { min: minValue - padding, max: maxValue + padding };
}

function chartTicks(domain: { min: number; max: number }): number[] { return Array.from({ length: 5 }, (_, index) => domain.max - ((domain.max - domain.min) * index) / 4); }
function chartTickIndexes(length: number): number[] { if (length <= 1) return length ? [0] : []; return Array.from({ length: Math.min(5, length) }, (_, index) => Math.round((index * (length - 1)) / (Math.min(5, length) - 1))); }
function chartX(index: number, length: number, plot: { left: number; right: number }): number { return plot.left + (index / Math.max(length - 1, 1)) * (plot.right - plot.left); }
function chartY(value: number, domain: { min: number; max: number }, plot: { top: number; bottom: number }): number { return plot.bottom - ((value - domain.min) / (domain.max - domain.min || 1)) * (plot.bottom - plot.top); }
function formatAxisTick(value: number, format: ChartAxis["format"]): string { if (format === "price") return value.toFixed(2); if (format === "percent") return `${value.toFixed(2)}%`; if (format === "score") return Math.round(value).toString(); return Math.round(value).toLocaleString("zh-TW"); }
function formatChartDate(value?: string): string { const match = String(value || "").match(/\d{4}-(\d{2})-(\d{2})/); return match ? `${match[1]}/${match[2]}` : String(value || "").slice(0, 10); }
function chartSegments(series: ChartSeries & { axis: "left" | "right" }, axis: "left" | "right", leftDomain: { min: number; max: number }, rightDomain: { min: number; max: number } | null, plot: { left: number; right: number; top: number; bottom: number }, timelineLength: number): string[] { const domain = axis === "right" && rightDomain ? rightDomain : leftDomain; const segments: string[] = []; let current: string[] = []; series.values.forEach((point, index) => { const value = point.value; if (value == null || !Number.isFinite(value)) { if (current.length > 1) segments.push(current.join(" ")); current = []; return; } current.push(`${chartX(index, timelineLength, plot)},${chartY(value, domain, plot)}`); }); if (current.length > 1) segments.push(current.join(" ")); return segments; }
function Metric({ title, value, accent }: { title: string; value: number | string; accent: string }) { return <div className={`metric ${accent}`}><span>{title}</span><strong>{typeof value === "number" ? value.toLocaleString() : value}</strong></div>; }
function Coverage({ coverage, large = false }: { coverage: Record<string, any>; large?: boolean }) { const keys = ["InstitutionalDataAvailable", "ForeignHoldingDataAvailable", "HoldingDistributionAvailable", "BrokerDataAvailable", "PriceDataAvailable"]; const count = keys.filter(k => coverage[k]).length; return <span className={large ? "coverage large" : "coverage"} title={keys.map(k => `${k}: ${coverage[k] ? "available" : "missing"}`).join("\n")}>{count}/5 sources</span>; }
function formatNumber(value: any) { return value == null || Number.isNaN(Number(value)) ? "—" : Number(value).toLocaleString("zh-TW", { maximumFractionDigits: 2 }); }
function formatDate(value?: string) { return value ? new Date(value).toLocaleString("zh-TW", { timeZone: "Asia/Taipei" }) : "—"; }
function scoreClass(status: string) { return ({ STRONG_ACCUMULATION: "strong", ACCUMULATION: "accumulation", WATCH: "watch", DATA_INSUFFICIENT: "insufficient" } as Record<string, string>)[status] || "neutral"; }
async function fetchJson<T>(url: string, signal?: AbortSignal, init?: RequestInit): Promise<T> { const response = await fetch(url, { ...init, signal }); if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json() as Promise<T>; }

createRoot(document.getElementById("root")!).render(<App />);
export default App;
