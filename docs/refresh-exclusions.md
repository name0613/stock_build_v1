# 排除股票管理與手動恢復

首頁「高可信建倉」右側的「排除股票」會進入「自動補抓排除清單」。搜尋代碼／名稱、選擇市場，使用每頁 50 筆的後端分頁瀏覽全部紀錄；返回原榜單會保留榜單、搜尋、篩選及頁碼。清單讀取只查資料庫，不呼叫 FinMind。

每檔按「補抓並評分，恢復自動補抓」後，由原本的手動優先佇列及 worker 共用 provider 鎖執行。畫面可追蹤排隊、執行階段、等待額度／供應商，以及完成結果；重新整理後會找回進行中作業。移出清單後會在結果摘要保留本頁作業與最近 20 筆完成紀錄，完整稽核保存在資料庫。

## 資料與解除規則

- 唯一排除依據仍是 `StockRefreshIssue.no_data_attempts >= REFRESH_NO_DATA_LIMIT`（目前為 5），與 status 字串或是否有分數無關。清單、自動跳過、每日完成統計共用同一條件。
- 排隊、執行中、額度／供應商等待、worker 中斷待續跑時保留排除。成功、`DATA_INSUFFICIENT`、最終失敗都會在同一交易內寫入最終作業狀態、解除時間並把有效次數設為 0。請求驗證或作業建立失敗不修改排除。
- 復原作業不計入新一輪失敗；下一次符合原條件的自動失敗從 1/5 開始，重新達 5 次仍會排除。一般手動補抓及 `_mark_refresh_recovered` 保留原本跨作業累計語意。
- 補抓重用有效來源資料及 checkpoint，重試空資料／缺失標記；分點依本機已驗證觀察重用，只重試缺口。沿用既有 s-only 與 capital-aware 持久化。缺資料保留原始資料並標示不足，不補零。回退評分會顯示實際評分日期及最新目標仍未完成。
- 獨立的 `refresh_exclusion_recoveries` 表以 `job_id` 為主鍵，保存原次數、status、原因、details、首次／最近嘗試時間、原 job_id、請求時間、結果及解除時間。這些欄位不放在可被覆寫的 JobRun checkpoint；舊完成作業重播不會再次歸零。
- 同股票進行中手動作業會被重用，專用復原請求會把解除意圖合併到該作業。PostgreSQL advisory transaction lock／SQLite immediate transaction 負責跨程序序列化建立與收尾。
- 自動排程在既有節點重新計算待完成股票；休市、額度、來源規則不變。優先手動作業返回批次時刷新 Session 狀態，後續輪替可納入剛恢復但尚未完成的股票。歷史作業跳過數及完成快照保留，清單另外顯示即時統計。

## API

- `GET /api/refresh-exclusions?search=&market=&page=1&page_size=50`：`total` 是全體排除數，`filtered_total` 是篩選結果數；`items`、`active_jobs`、`recent_results`、`daily_completion` 均來自本機資料庫。單頁最多 200 筆，總頁數無 200 筆限制。未保存的來源欄位回傳 null，前端顯示「未記錄」。
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

`test_refresh_exclusions.py` 覆蓋分頁與舊 status、API 無下載、作業合併／並發、等待與重啟、實際個股評分持久化、資料不足／回退／失敗、原子收尾中斷、舊作業重播、重新計次、一般補抓語意、歷史資料保留及自動排程重新納入。前端新增 7 項 E2E，並以 local config 執行既有榜單／個股操作回歸；使用可重現 API fixtures，不呼叫真實 FinMind 或修改正式資料庫。


2026-09-28 本機驗證結果：後端全套 263 項通過（新增本功能 22 項）；前端 E2E 26 項通過（新增 7 項）；TypeScript lint、production build、新增後端檔案 Ruff 與 Git whitespace 檢查通過。桌面截圖已檢視。後端使用隔離 SQLite，前端使用 API fixtures；本次未連接正式 PostgreSQL／NAS、未執行真實 FinMind 下載，也未部署正式環境，因此正式 PostgreSQL migration 與供應商實際回應仍需部署後驗證。
