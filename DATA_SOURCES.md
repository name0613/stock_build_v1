# Data sources and capability boundary

| Dataset | Role | Allowed in score | Freshness semantics |
|---|---|---:|---|
| `TaiwanStockInfo` | dynamic universe, name, market, industry, security type | reference only | source date from response |
| `TaiwanStockInstitutionalInvestorsBuySellWide` | foreign / trust / dealer buy-sell net | S1 | daily source date |
| `TaiwanStockInstitutionalInvestorsBuySell` | not enabled in this release | rejected; Wide is the only operational institutional source | n/a |
| `TaiwanStockShareholding` | foreign actual shares and ratio | S2 | daily source date |
| `TaiwanStockHoldingSharesPer` | holding levels, people, percent, shares | S3 | latest weekly source date |
| `TaiwanStockTradingDailyReport` | broker branch daily rows | S4 | daily source date |
| `TaiwanStockTradingDailyReportSecIdAgg` | Sponsor/SponsorPro capability probe and aggregate path | S4 equivalent | daily source date |
| `TaiwanStockPrice` | price, volume, return, price impact modifier | supporting only | daily source date |
| `TaiwanSecuritiesTraderInfo` | broker id -> broker name | reference only | reference source date |

The following are explicitly forbidden in this release: block trades, active ETF holdings, margin, securities lending, short-sale balances, government bank flow, industry-chain money flow, price tick, futures, options and US stocks. The client rejects them before an HTTP request.

## Sponsor / SponsorPro

`FinMindClient.probe()` makes a real, server-side capability probe and writes sanitized `FINMIND_CAPABILITY_EVIDENCE.json`. The implementation does not assume SponsorPro. The raw institutional dataset is deliberately removed from the operational allowlist until an equivalent historical-schema normalization is proven; Wide is never combined with raw rows. If a market-wide object is unavailable, `fetch_broker_stocks()` uses a bounded queue, semaphore, rate limiter, retry/backoff/jitter, Retry-After, request metrics and an atomic JSON checkpoint that allows resume after restart.

## 5%+ disclosure

`MajorShareholderDisclosure` is reserved for an official, stable, legal machine-readable TWSE/TPEx/MOPS source. No brittle DOM scraper is included. Until one is separately validated, the UI and documentation report unavailable instead of filling zero.
