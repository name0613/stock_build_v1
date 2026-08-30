# Taiwan Stock Accumulation Evidence

台股上市、上櫃、興櫃普通股的法人／大型資金「持續、分散、低調」建倉證據監控系統。系統只把資料解讀為 `Accumulation Evidence`，不宣稱「主力一定在買」、內線、保證上漲或買進建議。

## 目前範圍

- Universe 動態來自 FinMind `TaiwanStockInfo`；依市場與普通股欄位篩選，不把代碼硬寫死。
- S-only scoring：三大法人 Wide、外資實際持股、集保持股級距、券商分點；原始法人 dataset 在等價 normalization 完成前明確拒絕，不假稱為 fallback。
- `TaiwanStockPrice` 只作顯示、成交量 normalization、價格影響 modifier；`TaiwanSecuritiesTraderInfo` 只作券商名稱對照。
- 分點是券商營業據點彙總，不等同於單一投資人或「主力」。
- 缺重要資料時為 `DATA_INSUFFICIENT` 且 score 為 `NULL`，不把 missing 轉成 0。
- 5%+ 重大持股申報 schema 已保留；在確認官方、穩定、合法 machine-readable source 前不偽造、不補 0。

## 本機啟動

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force secrets | Out-Null
Set-Content secrets/postgres_password (python -c "import secrets; print(secrets.token_urlsafe(24))")

# backend unit/integration tests
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
$env:PYTHONPATH = "backend"
.venv\Scripts\python.exe -m pytest backend\tests -q

# frontend build
cd frontend
npm install
npm run build
```

Docker 需要 Docker Engine／Compose v2。部署前把 `FINMIND_API_TOKEN` 只放在執行環境或未被 Git 追蹤的 `.env`；不能貼到聊天、source、frontend 或 bundle。

```powershell
docker compose build
docker compose up -d
docker compose ps
```

預設 Web port 是 `18080`；若 NAS 該 port 已占用，以 `WEB_PORT` 指定已驗證未使用的 LAN port。PostgreSQL 僅在 internal network；API 為了執行單股 FinMind 補抓而同時使用 internal 與 public egress bridge，但不發布 API port，LAN 只看到 nginx。

## NAS deployment

不要猜 NAS volume path。部署腳本會先讀 OS／架構／Docker／Compose／磁碟／port／containers／volumes／networks，再在已存在的容器 volume (`/volume1/docker`, `/share/Container`, `/share/CACHEDEV1_DATA/Container`, `/mnt/user/appdata`) 中選擇第一個已驗證目錄；找不到就停止。

```powershell
$env:NAS_HOST = "192.168.31.138"
$env:NAS_USER = "Name"
$env:NAS_PASSWORD = ... # 由 secret manager 注入，勿寫檔
$env:FINMIND_API_TOKEN = ... # 由 secret manager 注入，勿寫檔
python -m pip install paramiko
python scripts/deploy_nas.py
.\scripts\verify_deployment.ps1 -Url http://192.168.31.138:18080
```

`deploy_nas.py` 不會輸出密碼、token 或 Authorization header；NAS `.env` 與 PostgreSQL secret 設為 `0600`，且不會被 Docker image 包入。

## Initial backfill / daily jobs

Worker 啟動時在來源發布窗口已開啟後才做完整 catch-up：動態 universe、四個 full-market S/reference source、當日券商 bounded queue、全 universe feature/score，並以 JobRun 記錄每階段狀態；工作日 21:30 做主更新，23:00 做補抓／retry，台股交易日 09:00–13:00 每 30 分鐘做一次開市同步，時區為 `Asia/Taipei`。開市同步只刷新當前交易日需要的法人、外資持股與價格資料，不會重跑完整 20D 分點 catch-up 或 Score。開市同步會再以交易日曆檢查 `OPEN`，假日與收市時不發出資料請求。若在來源窗口前啟動，worker 只維持健康 heartbeat 並標示 `DEFERRED_BEFORE_SOURCE_PUBLICATION`，不發出夜間批次資料請求。runtime heartbeat 同時標示台股連續交易時段 `OPEN`/`CLOSED`；閉市或週末沒有新行情是預期狀態，不會被當成 provider failure，開市時才標示 `monitoring_active=true`。批次來源日依 `completed_source_end_date` 決定：21:00 前不宣稱當日來源已發布，週末／假日回到最近已完成交易日。前端每 30 分鐘先讀取 worker health，只有確認 `OPEN` 才重新載入 holdings、summary、股票清單與個股明細。完整 backfill 應在 NAS 以 quota-aware checkpoint 工作執行；分點走 bounded queue、bounded concurrency、rate limiter、exponential backoff、jitter、Retry-After 與 atomic checkpoint/resume，不以無限呼叫換取 coverage。來源狀態若不完整仍會以 `PARTIAL`／`QUOTA_EXHAUSTED` 等狀態觀測與排程，但不再作全市場 Score veto；worker 對每檔股票獨立執行 readiness，只有通過完整資料合約的股票產生 numeric Score，其餘維持 `DATA_INSUFFICIENT`。`/api/readiness` 與 Score JobRun checkpoint 會記錄 ready、not-ready、score processed 及缺失原因分布。每週持股資料若尚未發布則以 `WAITING_FOR_PROVIDER_PUBLICATION` 持久化並節流全市場檢查；節流期間會用固定一檔股票做低成本 canary，發現目標週已發布後立即使舊等待狀態失效，改為 `HOLDING_PUBLICATION_PARTIAL`，再以 checkpoint/resume 逐檔補齊 15 個標準級距。

## Data status / troubleshooting

- `/health`：API／DB health。
- `/api/data-status`：每個 dataset 的 status、source date、last successful sync、job runs 與安全錯誤碼。
- `POST /api/score/current`：只用目前 PostgreSQL 已寫入的來源資料建立背景評分作業，不呼叫 FinMind；以 `GET /api/score/current?job_id=<id>` 查詢進度與結果。
- `GET /api/finmind/quota`：即時讀取已驗證帳號的 FinMind 每小時可用額度，只回傳去敏後的 used／remaining／limit／plan。
- `POST /api/favorites/fetch-and-score`：把按下按鈕當下的「我的最愛」依現有數值評分由高到低固化為持久佇列，逐檔強制重抓五項來源並重評；以同路徑 `GET` 查詢進度。額度不足會進入 `WAITING_FOR_QUOTA`，worker 每分鐘檢查並從未完成股票與資料集自動續跑。
- `/api/readiness?stock_id=<代碼>`：單股 side-effect-free 診斷，列出缺少的評分欄位、來源日期與穩定缺失原因；若最新目標日尚未發布但較早資料日已完整，也會回傳 `latest_ready_source_date`。
- `/api/docs`：API schema。
- `ACCESS_DENIED` 代表 plan permission，不代表沒有資料；`SCHEMA_MISMATCH` 代表欄位漂移，不會 silent ingest。
- Raw Parquet 在 `/data/raw/<dataset>/date=YYYY-MM-DD/`，metadata sidecar 保存 source、parameters（不含 token）、source date、fetched_at、SHA256。

## Backup / restore

備份 PostgreSQL volume 與 `/data/raw`，並將 manifest 與 score version 一起保存。恢復時先停止 worker，還原 volume，再啟動 postgres、API、worker；恢復後檢查 `/health`、row counts、latest source dates、score version 與 bundle evidence。不要刪除無關 container／volume，也不要執行 broad prune。
