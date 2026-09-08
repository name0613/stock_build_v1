# Operations

Worker schedules catch-up on startup, Monday-Friday 21:30 main sync and 23:00 retry. The open-market 30-minute path refreshes only current-session institutional/shareholding/price sources; it does not launch the full 20-day broker catch-up or Score loop. Dataset freshness is tracked separately; weekly holding data is not copied forward as a fake daily observation.

Inspect:

- `/api/data-status` for dataset state and job history.
- `/api/summary` for universe count and status counts.
- `/api/finmind/quota` for the sanitized live provider allowance.
- `/api/favorites/fetch-and-score` for the durable score-ordered favorites refresh queue.
- `/api/universe/refresh-and-score` for the durable missing-first, oldest-next universe queue with an exact 3,500 FinMind data-request budget per click.
- Parquet metadata sidecars for source parameters/date/fetch time/hash.
- `FINMIND_CAPABILITY_EVIDENCE.json` for actual capability probes bound to the full deployed source revision and provider/dataset policy hashes.
- `BROKER_SOURCE_ISOLATION_EVIDENCE.json` for prohibited-row/revision counts, the database constraint, quarantine counts and authoritative rebuild state.

Statuses are `RUNNING`, `SUCCESS`, `REUSED`, `PARTIAL`, `FAILED`, `QUOTA_EXHAUSTED`, `WAITING_FOR_PROVIDER_PUBLICATION` and `SCORE_BLOCKED_BY_SOURCE_COVERAGE`; `REUSED` means every expected observation was previously verified without a new physical provider request. Valid provider no-data is distinct from an unverified empty response and is scoped to explicit observation dates; unreturned sessions, partial ranges and incomplete pagination are retryable. Broker retries first read the authenticated provider quota, preserve the configurable `BROKER_QUOTA_RESERVE`, select only the usable pending budget, and persist `next_eligible_retry_at` with classified backoff. Weekly holding publication waits are persisted with the target date, last provider check, check result, query type and next eligible check. A wait is throttled for full-market requests, but a deterministic single-stock canary revalidates it; observed publication invalidates stale wait knowledge and moves to `HOLDING_PUBLICATION_PARTIAL` until every stock passes all 15 canonical buckets. Global source coverage is advisory for display: each stock is evaluated independently, and list/ranking/detail surfaces show its latest persisted numeric Score while stocks without a valid point-in-time input contract remain `DATA_INSUFFICIENT`. Coverage counters must reconcile expected, verified, unresolved, newly fetched, reused, valid no-data, retryable, permanent and physical requests. Error codes include `ACCESS_DENIED`, `RATE_LIMITED`, `UPSTREAM_5XX`, `TIMEOUT`, `SCHEMA_MISMATCH`, `INCOMPLETE_PROVIDER_COVERAGE`, `HOLDING_BUCKETS_INCOMPLETE`, `HOLDING_PUBLICATION_PARTIAL`, `WAITING_FOR_PROVIDER_PUBLICATION` and `RAW_STORAGE_UNAVAILABLE`.

User-triggered favorites refreshes add `QUEUED`, `WAITING_FOR_QUOTA`, and `WAITING_FOR_PROVIDER`. Their parent `JobRun` preserves the original score-descending stock order, completed stock IDs, per-dataset completion, current stock, quota snapshot, and next retry time. Worker restarts return an interrupted favorites job to `QUEUED` and resume it; scheduled full/intraday sync and favorites refresh share a provider-work lock so they do not spend quota concurrently inside the worker.

Never run `docker system prune`, delete unknown volumes, or stop unrelated services. Broker checkpoint files make retries resumable and idempotent.

## Capital-aware rankings

`/api/rankings?kind=stealth` is the historical S-only view. The two v7 views
are `/api/rankings?kind=large_capital` and
`/api/rankings?kind=high_confidence`; their responses include S/L/C/E, the
selected score, fixed formula hash, source date, knowledge cutoff, 20D median
Trading_money, estimated institutional net value, ratio, confirmation count
and gate reasons. `/api/summary` reports v7 scorable, data-insufficient and
gate-excluded counts. A v7 score is only regenerated from persisted source
rows by the same PIT scoring job; missing formal Trading_money is not filled.


## 休市每小時自動補抓與評分

Worker 以 Asia/Taipei 時區在每小時整點檢查交易日曆，只有 CLOSED 才建立自動作業（週末及日曆內的休市日也會執行）；OPEN 或 UNKNOWN 不建立、不續跑自動作業。與手動「使用 3,500 額度補抓並評分」共用缺資料優先佇列、逐檔刷新與評分、3,500 次資料請求上限及額度等待機制。既有未完成作業優先續跑，不另開一輪，同一整點時段重複觸發也不重建。

每分鐘 dispatcher 領取佇列及重試；自動作業每次 HTTP 請求（含 quota probe、重試）及評分前重新確認休市，跨入開市時停止新請求並保存進度與預算，已送出的請求可完成並保存資料。開盤暫停不累計股票的無資料失敗次數；手動作業保留原有行為。自動輪次以最新已收盤交易日為目標，收盤後即可嘗試當日來源；尚未發布的來源保持待完成。既有夜間同步及開市輕量同步保持原排程，與本功能共用 worker provider lock。

重啟後從下一整點建立新輪次，未完成自動作業仍受休市限制並由 dispatcher 接續。健康檢查註冊 market-closed-hourly-refresh，API 回傳 trigger / schedule_hour / phase / budget；前端會自動發現排程作業並顯示進度。


## 手動單股優先佇列

單股補抓 POST 現在回傳 202 / QUEUED，同檔未完成請求會回傳原作業，不因全市場批次的單股子作業回傳 409。手動請求使用 manual_stock_refresh_score 持久化，由 worker 共享 provider lock 執行，API 不再啟動背景抓取。3500 與我的最愛批次在兩檔股票之間優先處理手動 FIFO 佇列，再接回原批次；已完成的股票、資料集及 3500 額度進度保留。夜間與盤中同步也在開始及結束時讓手動佇列先行。

手動單股不受自動作業的開市暫停限制，但同樣遵守供應商剩餘額度與保留額度；不足或暫時失敗會保存來源進度，五分鐘後自動重試。worker 重啟會將中斷手動請求恢復 QUEUED。前端顯示已排隊／補抓中／等待額度並持續輪詢，重新進入個股頁也會恢復追蹤該股的手動作業。

若 worker 啟動時仍有持久化刷新工作，先啟動排程器接回佇列，延後啟動時的完整 catch-up，避免既有工作被夜間啟動批次長時間擋住。


## 自動補抓的每日完成條件

休市自動排程每小時使用最多 3,500 次資料請求，僅將未完成股票入列。用完一輪預算只代表 budget_completed，並非當天完成；後續整點持續建立下一輪，直到所有有效普通股皆完成。既有累計至少 5 次抓不到資料的股票不計入有效股票。手動 3,500 按鈕維持原有固定預算流程。

自動作業採 closed_market_target_date：交易日 13:30 收盤後目標為當天，盤前、週末及假日為最近已收盤交易日；跨午夜不把目標退回，新的收盤交易日會重建待抓名單、清除舊日期的來源續跑標記，保留已花費的本輪額度。收盤至原有 21:00 發布時間窗之前，當日資料缺漏不累計為永久跳過股票的失敗。

完成判定以實際資料庫為準：每檔最新一筆目前版本 S 評分必須是目標日的數值結果，具正確公式與輸入快照，五個來源皆達應有日期（持股分布採該週日期），而來源寫入時間不能晚於評分的 knowledge_cutoff。舊日期 fallback、DATA_INSUFFICIENT、資料更新後尚未重評分，皆仍是待完成。正常評分流程同時更新 capital-aware 結果。

全部達標即標示 daily_target_completed，即使還有剩餘額度也立即停止補抓。後续整點只讀本地完成狀態，不再呼叫 FinMind 或建立重複完成作業；新交易日、新增股票或來源修訂導致需要重新評分時恢復。前端 daily_completion 顯示目標日、上次檢查完成／待完成／排除檔數，避免將本輪額度用完誤認為全市場已完成。原有手動優先佇列與開市暫停保護仍適用。
