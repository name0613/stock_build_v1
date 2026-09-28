import { useEffect, useRef, useState } from "react";

type RecoveryJob = {
  job_id: number; stock_id: string; status: string; phase?: string; target_date?: string;
  evaluated_source_date?: string; fallback_applied?: boolean; next_retry_at?: string; error_code?: string;
  progress?: { completed: number; total: number }; score?: { score?: number | null; source_date?: string };
  readiness?: { missing_reasons?: string[] }; target_readiness?: { missing_reasons?: string[] };
  recovery?: { released_at?: string; automatic_refresh_eligibility: string };
};
type Exclusion = {
  stock_id: string; stock_name: string; market: string; no_data_attempts: number; attempt_limit: number;
  reason?: string; missing_sources?: string[]; last_attempt_at?: string; job?: RecoveryJob;
};
type Exclusions = {
  total: number; filtered_total: number; items: Exclusion[]; active_jobs: RecoveryJob[]; recent_results: RecoveryJob[];
  daily_completion?: { target_date: string; excluded_count: number; pending_count: number; completed_count: number };
};
const active = new Set(["QUEUED", "RUNNING", "WAITING_FOR_QUOTA", "WAITING_FOR_PROVIDER"]);
const statuses: Record<string, string> = { QUEUED: "排隊中", RUNNING: "執行中", WAITING_FOR_QUOTA: "等待額度", WAITING_FOR_PROVIDER: "等待供應商", SUCCESS: "補抓／評分完成", DATA_INSUFFICIENT: "資料不足（DATA_INSUFFICIENT）", FAILED: "最終失敗" };
const phases: Record<string, string> = { queued: "已排隊", queued_after_worker_restart: "重啟後續跑", quota_check: "檢查額度", quota_checked: "已檢查額度", preflight: "檢查資料", scoring: "立即評分中", ready_to_finalize: "保存結果中", completed: "已完成", failed: "已結束", waiting_for_quota: "等待額度", waiting_for_provider: "等待供應商" };
const sources: Record<string, string> = { TaiwanStockPrice: "股價／成交量", TaiwanStockShareholding: "外資持股", TaiwanStockInstitutionalInvestorsBuySellWide: "三大法人", TaiwanStockHoldingSharesPer: "集保持股", TaiwanStockTradingDailyReport: "分點", missing_price: "股價不足", missing_foreign_holding: "外資持股不足", missing_institutional: "法人資料不足", tdcc_required_buckets_incomplete: "集保持股週期或級距不足", missing_broker: "分點資料不足" };
const dateText = (value?: string) => value ? new Date(value).toLocaleString("zh-TW", { timeZone: "Asia/Taipei" }) : "未記錄";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, { cache: "no-store", ...init });
  if (!response.ok) throw new Error(String(response.status));
  return response.json();
}

function JobStatus({ job }: { job?: RecoveryJob }) {
  if (!job) return <>尚未提交</>;
  const missing = job.target_readiness?.missing_reasons || job.readiness?.missing_reasons || [];
  const phaseParts = (job.phase || "").split(":");
  const phase = phases[job.phase || ""] || (phaseParts.length > 1 ? `${phaseParts[0] === "fetching" ? "補抓" : phaseParts[0] === "reused" ? "沿用" : "已抓取"}：${sources[phaseParts[1]] || phaseParts[1]}` : job.phase);
  return <div className="recovery-status" data-testid={`recovery-job-${job.job_id}`}>
    <strong>#{job.job_id} · {statuses[job.status] || job.status}</strong>
    {active.has(job.status) && <span>{phase}{job.progress ? ` · ${job.progress.completed}/${job.progress.total}` : ""}</span>}
    <span>自動補抓資格：{job.recovery?.released_at ? "已恢復" : job.recovery ? "仍排除，作業結束後解除" : "仍排除（一般補抓不會解除）"}</span>
    {job.target_date && <span>目標資料日 {job.target_date}</span>}
    {!active.has(job.status) && <>
      <span>{job.score?.score == null ? "本次未取得數值評分" : `Score ${job.score.score.toFixed(1)} · 實際評分日 ${job.evaluated_source_date || job.score.source_date || "未記錄"}`}</span>
      {job.fallback_applied && <span className="action-error">採較早完整資料日評分；最新目標日尚未完成。</span>}
      {job.recovery?.released_at && <span>解除時間 {dateText(job.recovery.released_at)}</span>}
    </>}
    {missing.length > 0 && <span>缺失原因：{missing.map(reason => sources[reason] || reason).join("、")}</span>}
    {job.error_code && <span>原因：{job.error_code}</span>}
    {job.next_retry_at && active.has(job.status) && <span>下次嘗試 {dateText(job.next_retry_at)}</span>}
  </div>;
}

export function RefreshExclusions({ onBack, onChanged, onCount }: { onBack: () => void; onChanged: () => void; onCount: (count: number) => void }) {
  const [search, setSearch] = useState("");
  const [market, setMarket] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Exclusions | null>(null);
  const [completion, setCompletion] = useState<Exclusions["daily_completion"]>();
  const [completionError, setCompletionError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  const [jobs, setJobs] = useState<Record<string, RecoveryJob>>({});
  const [submitting, setSubmitting] = useState<string[]>([]);
  const busy = useRef(new Set<string>());
  const jobsRef = useRef(jobs);
  const callbacks = useRef({ onChanged, onCount });
  callbacks.current = { onChanged, onCount };
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    async function loadCompletion() {
      if (inFlight) return;
      inFlight = true;
      try {
        const next = await request<Exclusions>("/api/refresh-exclusions?page_size=1&include_completion=true", { signal: controller.signal });
        if (!controller.signal.aborted) { setCompletion(next.daily_completion); setCompletionError(false); }
      } catch {
        if (!controller.signal.aborted) setCompletionError(true);
      } finally { inFlight = false; }
    }
    void loadCompletion();
    const timer = window.setInterval(() => void loadCompletion(), 60000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [revision]);

  function remember(next: RecoveryJob[]) {
    const merged = { ...jobsRef.current };
    for (const job of next) {
      const previous = merged[job.stock_id];
      if (!previous || previous.job_id <= job.job_id) merged[job.stock_id] = job;
    }
    jobsRef.current = merged;
    setJobs(merged);
  }

  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    setLoading(true);
    async function load() {
      if (inFlight) return;
      inFlight = true;
      try {
        const params = new URLSearchParams({ search, market, page: String(page), page_size: "50" });
        const next = await request<Exclusions>(`/api/refresh-exclusions?${params}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        const returnedJobs = [...next.recent_results, ...next.active_jobs, ...next.items.flatMap(item => item.job ? [item.job] : [])];
        const finished = returnedJobs.some(job => !active.has(job.status) && active.has(jobsRef.current[job.stock_id]?.status));
        remember(returnedJobs);
        setData(next); setError(""); callbacks.current.onCount(next.total);
        if (finished) { callbacks.current.onChanged(); setRevision(value => value + 1); }
        const maxPage = Math.max(1, Math.ceil(next.filtered_total / 50));
        if (page > maxPage) setPage(maxPage);
        // Use the existing stock/job_id contract for progress, including off-page jobs.
        for (const job of Object.values(jobsRef.current).filter(job => active.has(job.status))) {
          const updated = await request<RecoveryJob>(`/api/stocks/${encodeURIComponent(job.stock_id)}/fetch-and-score?job_id=${job.job_id}`, { signal: controller.signal });
          if (controller.signal.aborted) return;
          remember([updated]);
          if (!active.has(updated.status)) { callbacks.current.onChanged(); setRevision(value => value + 1); }
        }
      } catch {
        if (!controller.signal.aborted) setError("排除清單或作業進度讀取失敗，請重試；背景作業仍會繼續。");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
        inFlight = false;
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 3000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [search, market, page, revision]);

  async function recover(stockId: string) {
    if (busy.current.has(stockId) || active.has(jobsRef.current[stockId]?.status)) return;
    busy.current.add(stockId); setSubmitting([...busy.current]);
    setActionErrors(current => ({ ...current, [stockId]: "" }));
    try {
      remember([await request<RecoveryJob>(`/api/refresh-exclusions/${encodeURIComponent(stockId)}/recover`, { method: "POST" })]);
      setRevision(value => value + 1);
    } catch {
      setActionErrors(current => ({ ...current, [stockId]: "提交未確認，請重新讀取作業狀態後重試。重送會沿用同一進行中作業。" }));
    } finally {
      busy.current.delete(stockId); setSubmitting([...busy.current]);
    }
  }
  const results = Object.values(jobs).filter(job => job.recovery && !active.has(job.status)).sort((a, b) => b.job_id - a.job_id);
  const currentCompletion = completion || data?.daily_completion;
  return <div className="app-shell exclusion-page">
    <header className="topbar"><button className="back-button" onClick={onBack}>← 返回原榜單</button><div><p className="eyebrow">REFRESH EXCLUSIONS</p><h1>自動補抓排除清單</h1></div></header>
    <main>
      <p className="notice">累計達門檻的股票會停止自動補抓。手動作業真正結束後，無論評分成功、資料不足或最終失敗，都會解除舊排除；後續自動失敗由 1/5 重新累計。</p>
      <section className="panel controls">
        <p data-testid="exclusion-count">全體排除 {data?.total ?? "—"} 檔 · 篩選結果 {data?.filtered_total ?? "—"} 檔</p>
        {currentCompletion && <p data-testid="exclusion-daily-completion">最近統計目標日 {currentCompletion.target_date} · 已完成 {currentCompletion.completed_count} · 待完成 {currentCompletion.pending_count} · 排除 {currentCompletion.excluded_count}</p>}
        {completionError && <p className="action-error">每日完成統計讀取失敗；可按重新讀取。</p>}
        <div className="control-row"><label>搜尋<input aria-label="排除股票代碼或名稱" value={search} placeholder="股票代碼／名稱" onChange={event => { setSearch(event.target.value); setPage(1); }} /></label><label>市場<select aria-label="排除股票市場" value={market} onChange={event => { setMarket(event.target.value); setPage(1); }}><option value="">全部</option><option>上市</option><option>上櫃</option><option>興櫃</option></select></label><button className="ghost-button" onClick={() => setRevision(value => value + 1)}>重新讀取</button></div>
      </section>
      {error && <p className="error-banner" role="alert">{error}</p>}
      {loading ? <p className="empty" role="status">正在載入排除清單…</p> : data && <section className="panel">
        {data.items.length === 0 ? <p className="empty">{data.total === 0 ? "目前沒有被排除的股票。" : "查無符合搜尋或市場篩選的排除股票。"}</p> : <div className="table-scroll"><table className="exclusion-table"><thead><tr><th>股票</th><th>市場</th><th>累計失敗／門檻</th><th>排除原因</th><th>已知缺失來源</th><th>最近嘗試</th><th>本次手動作業</th><th>操作</th></tr></thead><tbody>{data.items.map(item => {
          const job = active.has(jobs[item.stock_id]?.status) ? jobs[item.stock_id] : item.job;
          return <tr key={item.stock_id} data-testid={`exclusion-row-${item.stock_id}`}><td><strong>{item.stock_id}</strong><br />{item.stock_name}</td><td>{item.market}</td><td>{item.no_data_attempts}/{item.attempt_limit}</td><td>{item.reason === "INCOMPLETE" ? "必要來源不完整" : item.reason === "NO_DATA" ? "完全無資料" : "未記錄"}</td><td>{item.missing_sources?.length ? item.missing_sources.map(source => sources[source] || source).join("、") : "未記錄"}</td><td>{dateText(item.last_attempt_at)}</td><td><JobStatus job={job} /></td><td><button className="primary-button" disabled={submitting.includes(item.stock_id) || !!job && active.has(job.status)} onClick={() => void recover(item.stock_id)}>{submitting.includes(item.stock_id) ? "提交中…" : "補抓並評分，恢復自動補抓"}</button>{actionErrors[item.stock_id] && <p className="action-error" role="alert">{actionErrors[item.stock_id]}</p>}</td></tr>;
        })}</tbody></table></div>}
        <div className="pagination"><button disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一頁</button><span>第 {page} / {Math.max(1, Math.ceil(data.filtered_total / 50))} 頁 · 每頁 50 筆</span><button disabled={page * 50 >= data.filtered_total} onClick={() => setPage(value => value + 1)}>下一頁</button></div>
      </section>}
      {results.length > 0 && <section className="panel recovery-results" aria-label="復原結果摘要"><h2>復原結果摘要</h2><p>保留本頁本次執行及最近 20 筆完成紀錄；完整稽核紀錄保存在資料庫。此處資格表示該次解除結果，後續仍可能重新累計排除。</p>{results.map(job => <article key={job.job_id}><h3>{job.stock_id}</h3><JobStatus job={job} /></article>)}</section>}
    </main>
  </div>;
}
