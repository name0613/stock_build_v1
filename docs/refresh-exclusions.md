# 排除股票管理與手動恢復

首頁「高可信建倉」右側的「排除股票」會進入「自動補抓排除清單」。搜尋代碼／名稱、選擇市場，使用每頁 50 筆的後端分頁瀏覽全部紀錄；返回原榜單會保留榜單、搜尋、篩選及頁碼。清單讀取只查資料庫，不呼叫 FinMind。

每檔按「補抓並評分，恢復自動補抓」後，由原本的手動優先佇列及 worker 共用 provider 鎖執行。畫面可追蹤排隊、執行階段、等待額度／供應商，以及完成結果；重新整理後會找回進行中作業。移出清單後會在結果摘要保留本頁作業與最近 20 筆完成紀錄，完整稽核保存在資料庫。

## 資料與解除規則

- 唯一排除依據仍是 `StockRefreshIssue.no_data_attempts >= REFRESH_NO_DATA_LIMIT`（目前為 5），與 status 字串或是否有分數無關。清單、自動跳過、每日完成統計共用同一條件。
- 排隊、執行中、額度／供應商等待、worker 中斷待續跑時保留排除。成功、`DATA_INSUFFICIENT`、最終失敗都會在同一交易內寫入最終作業狀態、解除時間並把有效次數設為 0。請求驗證或作業建立失敗不修改排除。
- 復原作業不計入新一輪失敗；下一次符合原條件的自動失敗從 1/5 開始，重新達 5 次仍會排除。目標日必要資料恢復後，一般補抓及 `_mark_refresh_recovered` 也會清零；舊日期回退評分不算恢復。
- 補抓重用有效來源資料及 checkpoint，重試空資料／缺失標記；分點依本機已驗證觀察重用，只重試缺口。沿用既有 s-only 與 capital-aware 持久化。缺資料保留原始資料並標示不足，不補零。回退評分會顯示實際評分日期及最新目標仍未完成。
- 獨立的 `refresh_exclusion_recoveries` 表以 `job_id` 為主鍵，保存原次數、status、原因、details、首次／最近嘗試時間、原 job_id、請求時間、結果及解除時間。這些欄位不放在可被覆寫的 JobRun checkpoint；舊完成作業重播不會再次歸零。
- 同股票進行中手動作業會被重用，專用復原請求會把解除意圖合併到該作業。PostgreSQL advisory transaction lock／SQLite immediate transaction 負責跨程序序列化建立與收尾。
- 自動排程在既有節點重新計算待完成股票；休市、額度、來源規則不變。優先手動作業返回批次時刷新 Session 狀態，後續輪替可納入剛恢復但尚未完成的股票。歷史作業跳過數及完成快照保留，清單另外顯示即時統計。

## 2026-10 排除計次修正

- 依證交所 115 年開休市表補齊 2/12、2/13、2/27、9/28，交易日曆版本為 `tw-exchange-2026-v2`。
- 失敗只在來源發布窗口後按不同目標交易日計一次；同日跨作業、重啟或舊日期重播不增加次數。目標日必要資料恢復後連續失敗歸零。
- 以 `target_readiness` 判斷必要資料是否齊備。額外 8 週持股歷史缺口不會讓已達 4 週評分要求的股票被排除；舊日期 fallback 不代替目標日完整性。
- 確定的資料缺口本輪只嘗試一次，保存 `deferred_stock_ids`，不立即插回佇列；全數已處理但仍有缺口時以 `PARTIAL / waiting_for_source_data` 結束本輪，後續整點再試，3,500 為上限而非必須耗盡。
- 部署時使用 `scripts/reset_refresh_issues.py` 明確執行一次清零，先持久化備份，保留資料與歷史分數及手動復原收據，並重建未完成全市場佇列。相同 reset ID 重播不會再次清零。

## API

- `GET /api/refresh-exclusions?search=&market=&page=1&page_size=50`：`total` 是全體排除數，`filtered_total` 是篩選結果數；`items`、`active_jobs`、`recent_results` 均來自本機資料庫。另加 `include_completion=true` 才計算並回傳 `daily_completion`；前端每分鐘背景更新，作業完成後立即更新，避免全市場統計拖慢清單、搜尋與作業輪詢。單頁最多 200 筆，總頁數無 200 筆限制。未保存的來源欄位回傳 null，前端顯示「未記錄」。
- `POST /api/refresh-exclusions/{stock_id}/recover`：202，回傳作業契約與 `recovery`；可沿用既有 `source_date` 目標參數。排除已解除後的重送回傳最近一次復原收據；後續重新累積排除可再次建立新復原作業。
- `GET /api/stocks/{stock_id}/fetch-and-score?job_id=...`：沿用狀態查詢；`status` 是補抓／評分結果，`recovery.automatic_refresh_eligibility`／`released_at` 是該次解除結果。`target_readiness` 與 `evaluated_source_date` 分別呈現目標完整性與實際評分日。

## 啟用與驗證

更新 API、worker 及 frontend 到同一版本，重新啟動 API／worker。既有 `init_db()` 會自動套用 PostgreSQL `014_refresh_exclusion_recoveries.sql`；SQLite 開發／測試由 metadata 建立新表。無需修改環境變數、不回填舊排除、不刪除行情／籌碼／歷史分數。部署時須同步更新並重新啟動 worker，使其使用新的原子收尾邏輯。

Windows 本機驗證：

```powershell
.venv/Scripts/python.exe -m pytest backend/tests -q
npm --prefix frontend run lint
npm --prefix frontend run build
cd frontend
npx playwright test --config playwright.local.config.ts --workers=2 --reporter=line
```

`test_refresh_exclusions.py` 覆蓋分頁與舊 status、API 無下載、作業合併／並發、等待與重啟、實際個股評分持久化、資料不足／回退／失敗、原子收尾中斷、舊作業重播、重新計次、一般補抓語意、歷史資料保留及自動排程重新納入。前端新增 8 項 E2E，並以 local config 執行既有榜單／個股操作回歸；使用可重現 API fixtures，不呼叫真實 FinMind 或修改正式資料庫。


2026-09-28 本機驗證結果：後端全套 264 項通過（本功能 23 項）；前端 fixture E2E 27 項通過（本功能 8 項）；TypeScript lint、production build、新增後端檔案 Ruff 與 Git whitespace 檢查通過。一次全套測試出現 SQLAlchemy `after_transaction_end` TypeError，本功能單獨及全套重跑均通過，尚未重現；保留此測試環境觀察。

PostgreSQL 實證使用 `scripts/refresh_exclusions_postgres_probe.py`，在獨立臨時 schema 執行 migration 重入、跨程序去重、JSON 清單與意圖查詢、完成邊界回滾、原子解除、每日排程重新接手、舊作業重播等 7 項檢查，全部通過。測試結束刪除該臨時 schema，不修改正式股票或呼叫供應商。

正式環境唯讀 E2E：

```powershell
cd frontend
$env:E2E_BASE_URL='http://192.168.31.138:18080'
$env:EXCLUSIONS_LIVE_SMOKE='true'
npx playwright test refresh-exclusions-live.spec.ts --workers=1 --reporter=line
```

此測試檢查真實 API 清單、排除判斷、按鈕位置、進入、重新整理及返回，保存畫面截圖；不提交真實股票復原或消耗 FinMind 額度。供應商實際補抓與復原的完整流程仍以隔離測試驗證，未對正式股票提交復原驗收。

2026-09-28 NAS 已部署程式版本 `80d9f39a2972201adaebd0fbe6bf6f88dfb903bd`，網址 `http://192.168.31.138:18080/#refresh-exclusions`。API、worker、frontend 版本一致；API／worker 全部 Python 程式在換行正規化後與 Git 相符，靜態資源與本機 production build 相符。正式 migration 014 已套用，API、worker、Nginx、PostgreSQL 健康，原自動作業 #29495 已續跑。正式唯讀 E2E 1 項通過，截圖已檢視。

部署時驗證既有 backend requirements lock 相符後重用依賴層，完整複製已提交程式；前端使用本機 production build 製作 Nginx 映像。原全新 pip 建置停滯後已停止；封裝產生的靜態目錄讀取權限問題已在映像內修正並通過真實瀏覽器驗收。保留部署前映像與 NAS 本機回滾封存，既有 credentials、資料卷與使用者未提交的 evidence 檔案未覆蓋。

正式清單為 933 檔、每頁 50 筆，最近驗證 API 約 26 毫秒；每日統計在同時載入首頁／執行 worker 時約 8 秒，背景顯示不阻擋操作。全市場 2148 檔，排除 933、已完成 1212、待完成 3（目標日 2026-09-24）。已取得資料與歷史分數保留，自動 worker 繼續新增評分。驗證細節及映像／版本資料見 [`REFRESH_EXCLUSIONS_DEPLOYMENT_EVIDENCE.json`](../deployment_evidence/REFRESH_EXCLUSIONS_DEPLOYMENT_EVIDENCE.json)。


2026-10 日曆修訂使用獨立評分版本 `s-only-v6-calendar-v2`，綁定 `tw-exchange-2026-v2` 與新 manifest hash。舊 `s-only-v6` 的 manifest、分數與輸入快照保持不變；新增評分由補抓或本機重新評分產生。capital-aware-v7 的資金公式及門檻不變。
