# Data sources and capability boundary

| Dataset | Role | Allowed in score | Freshness semantics |
|---|---|---:|---|
| `TaiwanStockInfo` | dynamic universe, name, market, industry, security type | reference only | source date from response |
| `TaiwanStockInstitutionalInvestorsBuySellWide` | foreign / trust / dealer buy-sell net | S1 | daily source date |
| `TaiwanStockInstitutionalInvestorsBuySell` | not enabled in this release | rejected; Wide is the only operational institutional source | n/a |
| `TaiwanStockShareholding` | foreign actual shares and ratio | S2 | daily source date |
| `TaiwanStockHoldingSharesPer` | holding levels, people, percent, shares | S3 | latest weekly source date |
| `TaiwanStockTradingDailyReport` | broker branch daily rows | S4 | daily source date |
| `TaiwanStockTradingDailyReportSecIdAgg` | capability probe only; zero-row/non-equivalent path is not production-used | not used | n/a |
| `TaiwanStockPrice` | price, volume, formal Trading_money/Trading_turnover, return, price impact modifier | supporting for v6; primary liquidity/value input for v7 | daily source date |
| `TaiwanSecuritiesTraderInfo` | broker id -> broker name | reference only | reference source date |

The following are explicitly forbidden in this release: block trades, active ETF holdings, margin, securities lending, short-sale balances, government bank flow, industry-chain money flow, price tick, futures, options and US stocks. The client rejects them before an HTTP request.

## Sponsor / SponsorPro

`FinMindClient.probe()` makes exact broad and per-stock/per-session server-side capability probes and writes sanitized, source-revision-bound `FINMIND_CAPABILITY_EVIDENCE.json`. Production, reference and capability-only allowlists are separate. `TaiwanStockTradingDailyReportSecIdAgg` is reachable only through the private probe path: production fetch and ingestion reject it, it has no production model mapping, every broker scoring/API query is source-bound, and the database constrains `broker_daily` to the official `TaiwanStockTradingDailyReport` source. The implementation does not assume SponsorPro. The raw institutional dataset is deliberately removed from the operational allowlist until an equivalent historical-schema normalization is proven; Wide is never combined with raw rows. If a market-wide object is unavailable, `fetch_broker_stocks()` uses a bounded queue, semaphore, one global physical-attempt budget, retry/backoff/jitter, Retry-After, request metrics and an atomic JSON checkpoint that allows resume after restart. Empty responses are never classified as usable.

Daily checkpoint coverage is the set of verified Taiwan trading sessions. Holding-distribution coverage is the set of verified Friday publication observations. A returned row covers only its exact stock/date after normalization and ingestion acceptance; explicit valid no-data covers only the dates named by the validated provider semantic. Partial/truncated ranges keep every unresolved expected observation retryable.

## 5%+ disclosure

`MajorShareholderDisclosure` is reserved for an official, stable, legal machine-readable TWSE/TPEx/MOPS source. No brittle DOM scraper is included. Until one is separately validated, the UI and documentation report unavailable instead of filling zero.

## Capital-aware semantics

`Trading_money` is persisted exactly as supplied by FinMind in New Taiwan
dollars; it is never replaced by `close * Trading_Volume`. `Trading_turnover`
is persisted as the provider's transaction-count field. Missing formal values
keep VWAP, 20D value windows and dependent capital scores unavailable.
Institutional net-value features are calculated as daily net shares multiplied
by formal daily VWAP and are explicitly estimates, not actual cash costs.
Validated broker rows can confirm positive broker amount events, but a broker
branch is an execution-venue aggregate, not one investor, and omitted rows
remain unknown.
