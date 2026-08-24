# Codex 帳戶切換後續作業 Prompt

你現在是本專案唯一的 **Developer / Release Engineer / Production Safety Remediation Owner**。請直接接手目前工作並持續執行到 ChatGPT 的 Production Safety Review 明確回覆核准為止，不要只整理狀態、提出建議或把操作交還給使用者。

## 最高優先級責任

1. 實際檢查、修改、測試、部署與蒐集證據；不得只產出設計文件。
2. 若程式碼有任何變更，遵守 `AGENTS.md`：每次程式變更都要同步提交至 Git。
3. 最終將程式碼交給 **Codex 內建瀏覽器中已登入的 ChatGPT Production Safety Review 對話**審查時，以下操作全部由你（Codex）負責：
   - 產生最終 reviewer ZIP。
   - 驗證 ZIP 成員、manifest、雜湊、路徑安全與秘密掃描。
   - 在 Codex 內建瀏覽器開啟或沿用 Production Safety Review 對話。
   - 親自點擊附件按鈕並上傳 ZIP。
   - 親自把相關審查內容貼入訊息欄。
   - 親自點擊「送出」。
   - 等待並讀取 ChatGPT 回覆，依結果繼續修復或確認完成。
4. 不得要求使用者代為上傳檔案、貼上文字或點擊送出。只有遇到必須由使用者完成的登入／重新驗證，或需要重新提供秘密時，才可請使用者處理該最小步驟；登入完成後仍由你繼續所有瀏覽器操作。
5. 舊 reviewer bundle 已被審查並判定 `NOT_APPROVED`，不得重傳舊 ZIP。必須等所有必要證據通過後產生全新的 immutable bundle。
6. 不得虛構、補零、合成或手動竄改市場資料及證據。若證據仍是 `PARTIAL_NOT_PROVEN` 或 full-market 尚未完成，不得送審。

## 安全與秘密管理

- 專案路徑：`D:\大戶緩慢建倉`
- NAS：`192.168.31.138`
- NAS 使用者：`Name`
- NAS 專案：`/volume1/docker/tw-accumulation-evidence`
- Production Web：`http://192.168.31.138:18080`
- **本檔刻意不保存 NAS 密碼與 FinMind API Token。** 帳戶切換後，如目前程序環境沒有秘密，請要求使用者重新提供或在終端安全輸入。
- 不得把密碼或 Token 放進命令列參數、工具輸出、Git、證據 JSON、reviewer bundle 或這份 Markdown。
- 先前秘密曾以聊天訊息提供，應建議使用者在流程完成後輪替；若可立即輪替且不阻礙工作，也可先輪替再繼續。

PowerShell 安全取得 NAS 密碼的標準方式：

```powershell
$nasSecure = Read-Host -AsSecureString 'NAS password'
$nasPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($nasSecure)
try {
  Set-Item -LiteralPath Env:NAS_PASSWORD -Value ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($nasPtr))
  $env:NAS_HOST = '192.168.31.138'
  $env:NAS_USER = 'Name'
  .venv\Scripts\python.exe <script>
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($nasPtr)
  Remove-Item Env:NAS_PASSWORD -ErrorAction SilentlyContinue
}
```

FinMind Token 也必須用同類型的 `Read-Host -AsSecureString` 流程注入 `FINMIND_API_TOKEN`，並在 `finally` 清除。絕對不要在回覆或終端輸出中顯示秘密。

## 接手時必讀

開始操作前完整閱讀：

- `D:\大戶緩慢建倉\AGENTS.md`
- `C:\Users\Erlie\.codex\skills\finmind\SKILL.md`
- `C:\Users\Erlie\.codex\plugins\cache\openai-bundled\browser\26.814.41957\skills\control-in-app-browser\SKILL.md`

使用內建瀏覽器 skill 來檢查、上傳、輸入與點擊。不要只因 ambient browser context 顯示網址就假設頁面狀態；實際讀取頁面並確認。

## 目前權威版本

- Workspace：`D:\大戶緩慢建倉`
- Git HEAD：`45b1566fe7dc3262829351b402c5ccc08150d113`
- Commit：`Bind catch-up evidence to runtime revision`
- NAS Production 已部署相同精確 revision；API、worker、frontend image labels 均已核對一致。
- Score version：`s-only-v6`
- Formula hash：`21df6cf28188f6ef3973e8283cb6f482d9895203e9f023f3dc9c69bec926f99b`
- Calendar hash：`245382ea5cb79419654219e8eef2303664a79037765f12e10fdfc86361ef4e5b`
- Backend lock hash：`75a0d62812977f2137f3fdbd0082594ba87efef8b0ce8af3c7fbbd0c0a0dfc47`
- Frontend lock hash：`36c6571745da922fb5b37d65bf374a7fb146c28aadb7f55850a131399ff6ce0d`

近期重要 commits：

- `45b1566` Return full sanitized quota evidence from worker
- `d6d7f70` Add remote provider quota evidence probe
- `0c1ec58` Include sanitized JobRun details in runtime evidence
- `da23359` Continue broker queue after per-stock empty responses
- `b0e1e70` Capture live broker provider contract evidence

工作樹中有大量產生中的 evidence/test artifacts，屬於目前任務成果，不得 reset、checkout 或刪除。先用 `git status --short` 判讀；不要覆蓋使用者變更。

這份接手 Markdown 應維持為工作區 artifact，不要只為提交本檔而改變 HEAD，否則會破壞「部署 revision 與證據 revision 完全一致」的狀態。若後續真的改了程式碼，才依 `AGENTS.md` commit，並重新部署及重做所有與 revision 綁定的證據。

## Production Safety Review 現況

Codex 內建瀏覽器先前有一個分頁：

- 標題：`Production Safety Review`
- URL：`https://chatgpt.com/c/6a87101e-0000-83ee-9c39-ae4791e010a4`

舊 bundle 已上傳，reviewer 回覆 `NOT_APPROVED`，核心缺口共九項：

1. Sponsor/full-market 完成證明。
2. 真實 quota renewal fairness。
3. 版本化 counters。
4. cleanroom 重建證明。
5. migration 證明。
6. broker 完整性／未知值語義。
7. capability 證明。
8. populated performance/browser metric。
9. 實際 JobRun，不可只有 skipped。

其中程式與證據層面的 3、4、5、6、7，以及 browser 搜尋／效能問題大致已完成。最新 exact revision 已重新部署，並完成 source-bound runtime、capability、broker contract、quota 證據重採集。真正仍未關閉的關鍵是：

- full-market ingestion 尚未全部完成。
- exact-revision 的 quota renewal／多輪公平性仍需最終證明。
- 必須讓 scoring job 實際執行並產生 current populated rankings。
- 完成後需在相同 revision 重跑所有 final evidence、acceptance 與 bundle 檢查。

### 最近一次 exact revision runtime（2026-08-21 Asia/Taipei）

- NAS source revision：`45b1566fe7dc3262829351b402c5ccc08150d113`，與本機 HEAD 一致。
- Runtime：API/worker healthy、`stale=false`、named volumes 存在。
- 市場狀態：`CLOSED`，`monitoring_active=false`；閉市不宣稱有盤中連續監控。
- 最新 catch-up JobRun：實際執行後以 `PARTIAL/QUOTA_EXHAUSTED` fail-closed 結束；沒有把未知或未取得資料補成零。
- 最新 provider quota：Sponsor、每小時 6000，22:40 probe 顯示已用 5441、剩餘 559；不應在 quota 不足時盲目重試。
- `NAS_DEPLOYMENT_EVIDENCE.json`、`FINMIND_CAPABILITY_EVIDENCE.json`、`BROKER_PROVIDER_CONTRACT_EVIDENCE.json`、`FINMIND_PROVIDER_QUOTA_EVIDENCE.json` 已重新綁定 `45b1566`。
- 目前不得上傳／送出舊 bundle；只有 full-market 與 scoring current ranking 完成後，才產生並上傳新的 immutable ZIP。

## 已完成的主要修復

- 版本化 current-attempt counters 與歷史保留。
- Broker `s-only-v6` 改為 unknown-not-zero；只使用實際觀測且通過驗證的 rows，不對未回傳分點推論零值、集中度或 spike。
- FinMind quota 可由 provider 直接 probe，輸出已去敏感化。
- Cleanroom 流程以 Git archive、no-cache build、backend tests、frontend tests/build、migrations、restart persistence 驗證。
- LAN browser acceptance 與效能 metrics。
- Concurrent score manifest race 修復。
- JSONB 與 legacy migrations 修復。
- PIT deadlock 修復。
- Source checkpoint v6：精確 expected observations；provider 成功但 filtered empty 只視為 missing coverage，絕不存成零或拿去 scoring；含 v5 migration、content hashes 與 metrics。
- Quota budget evidence 分離 source/broker version 並檢查 content continuity。
- Dockerfile 包含 contract fixtures。
- Frontend 搜尋 race 以 AbortController/request id 修復，並有 deterministic delayed-request regression test。
- Quota renewal evidence 僅在完整 cursor round-trip 且 `physical_requests + reused_complete + reused_valid_no_data >= requested` 時接受，已有 tests。
- Catch-up evidence 直接包含 runtime `source_revision`，已有 test。

## 已通過的重要驗證

- 最新本機 backend：`132 passed`。
- Ruff：PASS。
- Frontend test、lint、build：PASS。
- NAS Playwright 曾完成 `12/12` PASS；full-market 結束後仍需針對最終 exact state 重跑。
- 內建瀏覽器曾確認：`s-only-v6`、2148 stocks，搜尋 `2330` 只出現一筆台積電。
- Exact cleanroom `9b8cf25...`：no-cache、backend/frontend checks、10 migrations、restart persistence 全部 PASS，cleanup 已完成。
- Cleanroom evidence：`deployment_evidence/CLEANROOM_DOCKER_EVIDENCE.json`
- PIT PostgreSQL：歷史結果 bit-for-bit identical，較晚時間點結果不同，初始 score 非 null。
- Legacy migration：10 migrations、JSONB、counter reset、legacy broker 未被誤升級。
- Broker source isolation：PASS。
- Production named-volume persistence identical：PASS。
- Image secret evidence：PASS。
- Secret surface evidence：PASS。
- Local secret scan：PASS。
- Capability evidence：`deployment_evidence/FINMIND_CAPABILITY_EVIDENCE.json`
  - source revision 為 `9b8cf25...`
  - production per-stock 可用：Wide、Shareholding、HoldingSharesPer、官方 TradingDailyReport
  - SecIdAgg 僅 capability，`production_used=false`
- Direct provider quota evidence：source revision matched，Sponsor `6000/hour`。
- Runtime 曾確認 `ready=true`、`stale=false`、scheduler healthy。

## 已執行的真實資料週期

### 較早的 c793 週期（只作歷史進展，不可冒充 final exact revision）

2026-08-21 17:09–17:35（Asia/Taipei）：

- Wide unresolved：13423 → 5206；provider missing 9567。
- Shareholding unresolved：9507 → 2312；provider missing 8820。
- Holding：received 176375、accepted 155625、rejected 20750、versioned 20790、unresolved 4017。

### 第一輪 exact `9b8cf25` 週期

2026-08-21 18:11:04–18:37:12（Asia/Taipei）：

- Source-bound catch-up evidence 已確認 revision。
- TaiwanStockInfo：success 2148。
- Wide：requested 2148、physical 1967、reused_complete 2、reused_valid_no_data 179、rows accepted/versioned 1630、success 1827、unresolved 5206 → 2706；content hash continuous；無 zero imputation。
- Shareholding：physical 1980、reused_valid_no_data 168、success 1834、unresolved 2312 → 314；content hash continuous；無 zero imputation。
- Holding：physical 2027、success 1792、received 29308、accepted 25860、rejected 3448、versioned 24675、unresolved 4017 → 377；content hash continuous；無 zero imputation；最後因 quota exhausted 結束。
- Evidence：`deployment_evidence/CATCH_UP_ATTEMPT_EVIDENCE.json`
- 狀態仍為 `PARTIAL`，原因是配額耗盡，並非程式失敗。

當時 quota budget evidence：

- provider limit：PASS
- budget math：PASS
- durable checkpoint：PASS
- counters：PASS
- multi-renewal progress：PASS（含較早週期與 exact 週期）
- full-market completion：NOT_PROVEN
- overall：`PARTIAL_NOT_PROVEN`

2026-08-21 19:08:30 左右 provider remaining 仍為 0；監控程序已在切換帳戶前安全停止。預期 quota 約在第一輪開始後一小時恢復。接手時時間已更晚，所以第一件事是確認目前 runtime 是否已被 scheduler 或其他流程啟動，不得直接重複發動。

## 接手後立即執行順序

### 1. 檢查工作區、runtime 與是否已有 active JobRun

```powershell
Set-Location 'D:\大戶緩慢建倉'
git status --short
git rev-parse HEAD
$d = Invoke-RestMethod 'http://192.168.31.138:18080/api/data-status'
$d | Select-Object ready,stale,score_version,source_revision
$d.jobs | Select-Object -First 10 dataset,status,started_at,finished_at,records,stocks_completed,stocks_failed,error_code,@{n='checkpoint';e={$_.checkpoint_state | ConvertTo-Json -Compress -Depth 8}}
```

若已有 `RUNNING` ingestion/scoring job，先監看，絕對不要 restart、redeploy、recreate DB 或再啟動第二個 catch-up。

Scheduler 可能在台北時間 21:30 執行 main、23:00 retry；檢查 worker health 與 `next_expected_run_at`，避免與自動排程衝突。

### 2. 安全檢查 FinMind 即時配額

以 SecureString 注入 Token 後執行：

```powershell
.venv\Scripts\python.exe scripts\provider_quota_probe.py
```

確認輸出沒有洩漏 Token。若剩餘配額大於 0、沒有 active job，且距第一輪 exact cycle 開始已超過 55 分鐘，就可啟動下一輪 exact catch-up。

### 3. 啟動並完整監看第二輪 exact catch-up

以 SecureString 注入 NAS 密碼與 NAS 相關環境變數後執行：

```powershell
.venv\Scripts\python.exe scripts\run_catch_up_remote.py
```

啟動後持續監看直到該輪有明確 terminal status。不要中途停止。預期先完成剩餘 Wide／Shareholding／Holding，再推進 Price 與 broker。每輪都要核對：

- runtime `source_revision` 仍為 exact HEAD。
- JobRun 並非假性 skipped。
- checkpoint cursor 與 content hash continuity。
- unresolved observations 單調下降。
- `provider_missing` 仍保持 unknown-not-zero。
- 無資料不得被寫成 0 或進入 scoring。
- physical requests、reused complete、reused valid no-data 與 requested 的 round-trip 關係正確。

### 4. 依 quota window 重複，但不可盲目重試

每次 quota 恢復後，只有在沒有 active job 時才啟動下一輪。持續到五個 `SOURCE_DATASETS` 在 runtime 都為 `SUCCESS` 或可證明的 `REUSED`、unresolved=0，且 quota evidence `overall=PASS`。

每輪完成後更新：

```powershell
.venv\Scripts\python.exe scripts\quota_budget_evidence.py http://192.168.31.138:18080 `
  --provider-quota-evidence deployment_evidence\FINMIND_PROVIDER_QUOTA_EVIDENCE.json `
  --output deployment_evidence\QUOTA_BUDGET_EVIDENCE.json
```

若仍為 `PARTIAL_NOT_PROVEN`，繼續資料週期，不得包 reviewer bundle。

### 5. 確認 scoring 真正執行且排名有資料

Full-market 完成後，確認：

- 有實際成功的 scoring JobRun，而非只有 skipped。
- current ranking populated。
- score version、formula hash、calendar hash 與 manifest 全部一致。
- 不完整／未知 broker 或 source observation 未被當成 0。
- API 與 UI 都顯示最終資料，stock search 無 stale response race。

## Final exact evidence 重跑清單

只有 full-market 與 scoring 完成後才開始。所有 evidence 都必須綁定目前 exact Git/NAS image revision；任何 stale revision artifact 都要重跑，不能只改 JSON 文字。

1. 重跑 provider quota probe，確認 final evidence source match。
2. 蒐集 runtime：

```powershell
.venv\Scripts\python.exe scripts\collect_runtime_evidence.py http://192.168.31.138:18080
```

3. 在沒有 ingestion/scoring 執行時，以安全 NAS 憑證依序執行必要工具：

- `scripts/collect_nas_runtime_metadata.py`
- `scripts/run_nas_migration_evidence.py`
- `scripts/run_pit_postgres_remote.py`
- `scripts/run_broker_source_audit_remote.py`
- `scripts/collect_nas_acceptance_evidence.py`（此流程會 recreate PostgreSQL，只能在確定無 job 時執行）
- `scripts/collect_image_secret_evidence.py`
- `scripts/collect_secret_surface_evidence.py`
- `scripts/run_capability_probe_remote.py`（需要 quota 時先確認剩餘配額）

4. 重跑完整本機驗證：

```powershell
.venv\Scripts\python.exe -m pytest backend\tests -q --junitxml=test_results\backend-junit.xml
.venv\Scripts\ruff.exe check backend scripts
Set-Location frontend
npm test
npm run lint
npm run build
$env:E2E_BASE_URL = 'http://192.168.31.138:18080'
npx playwright test --reporter=json,junit
Set-Location '..'
```

5. 用 Codex 內建瀏覽器做最終視覺／互動驗收，確認 UI、版本、排名、搜尋與錯誤狀態；留下可重現、可核對的 browser evidence。
6. 更新或重跑所有 stale evidence，至少檢查：
   - BROWSER_ACCEPTANCE
   - FAIL_CLOSED_REMEDIATION
   - SOURCE_CHECKPOINT
   - HOLDING_SCHEMA
   - test/build results
   - current acceptance 與 manifest
7. 再做 local、image、surface secret scans，保證所有憑證不在任何 bundle 成員內。

## Reviewer bundle 產生與獨立驗證

只有下列條件全部成立才可產生新 bundle：

- full-market completion = PASS。
- quota budget overall = PASS。
- 真實 multi-renewal fairness = PASS。
- runtime、NAS image、Git revision 完全一致。
- 實際 scoring JobRun 成功且 ranking populated。
- backend/frontend/Playwright/cleanroom/migration/PIT/persistence/security 全部 PASS。
- 所有證據都不是舊 revision。
- secret scans PASS。

使用專案既有流程產生 fresh acceptance、manifest 與 bundle：

```powershell
.venv\Scripts\python.exe scripts\generate_reviewer_bundle.py
```

產生 ZIP 後必須再獨立驗證，不可只相信產生器：

- ZIP member set 與 manifest 完全相等。
- 每個 member SHA-256 與 manifest 相等。
- 無 absolute path、`..` traversal、重複名稱、symlink 或非預期成員。
- ZIP 內所有文字與 binary 做秘密掃描。
- 確認沒有 NAS 密碼、FinMind Token、Authorization header、cookie、session 或其他憑證。
- 確認所有聲稱的 revision/hash/status 可由 bundle 內證據重現。
- ZIP 建立並完成最終驗證後，再建立 detached SHA-256 sidecar；不可讓 sidecar 反過來改變 ZIP。
- 記錄新 ZIP 完整檔名、絕對路徑、大小及 SHA-256。

## 必須由 Codex 完成的 ChatGPT 送審流程

這一段是硬性要求，不得交給使用者：

1. 完整閱讀 browser control skill。
2. 使用 Codex 內建瀏覽器找到或開啟 `Production Safety Review` 對話。
3. 先讀取最新 reviewer 回覆，確認沒有遺漏的新要求。
4. 點擊附件／上傳控制項。
5. 選取本次新產生且已獨立驗證的 ZIP；不得選舊 bundle。
6. 等待附件完成上傳並確認檔名／大小正確。
7. 在訊息欄貼上 final review request，內容至少包含：
   - 請以 bundle 內 immutable manifest 與 evidence 為 source of truth。
   - 本次 exact Git/NAS image revision。
   - 新 ZIP SHA-256。
   - 舊版九項 blocker 對應的新證據路徑與 PASS 摘要。
   - 明確請 reviewer 回覆 `APPROVED` 或列出仍未通過的 blocker。
8. **由 Codex 親自點擊送出。** 不可停在「已填好、請使用者確認」。
9. 等待 ChatGPT 完整回覆；必要時持續輪詢頁面。
10. 讀取並整理 reviewer 結果：
    - 若 `APPROVED`：最後再確認 deployed revision 未變、runtime healthy，才向使用者宣告完成。
    - 若 `NOT_APPROVED`：把每個 blocker 映射到程式／證據，繼續實作、測試、部署、重做 bundle，再由 Codex 重複上傳與送出，直到核准。

除非 browser session 真的要求使用者重新登入，否則不得請使用者操作網頁。若需要登入，只請使用者完成登入，然後你立即接手後續附件、內容與送出。

## 不得做的事

- 不得因等待 quota 就宣告完成。
- 不得以 `PARTIAL`、`NOT_PROVEN`、`SKIPPED` 包裝成 PASS。
- 不得重傳舊 ZIP。
- 不得修改 evidence JSON 來假造測試或資料結果。
- 不得將 unknown data 填 0。
- 不得在 active job 中 restart/redeploy/recreate database。
- 不得刪除現有 generated evidence 或使用 `git reset --hard`／`git checkout --` 清掉工作樹。
- 不得在未 commit 的程式變更上部署；若程式有變更，先 commit，再以新 revision 重做所有 revision-bound 驗證。
- 不得把「上傳附件、貼內容、點擊送出」交給使用者。

## 完成定義

本任務只有在以下全部達成時才算真正完成：

1. 五個 source datasets full-market complete，無未解 observations。
2. 真實 quota renewal 與公平性證據 PASS。
3. 真實 scoring JobRun 成功、current ranking populated。
4. Production runtime healthy，Git/NAS image/evidence revision 一致。
5. 所有測試、安全、cleanroom、migration、PIT、persistence、browser acceptance PASS。
6. Fresh immutable reviewer bundle 通過獨立 member/hash/path/secret 驗證。
7. Codex 已親自透過內建瀏覽器上傳 ZIP、貼上內容並點擊送出。
8. Production Safety Review 明確回覆 `APPROVED`。

現在請從「檢查 runtime 是否已有 active JobRun、再安全確認 provider quota」開始，持續自主執行，不要只回報計畫。
