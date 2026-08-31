# Scoring

`score_version = s-only-v6`; the backend publishes the complete machine-readable formula specification at `/api/score-spec` and hashes every constant, transform, cap, threshold, window and missing-data rule into SHA-256 `formula_hash`. A material formula or source-policy change requires a new score version. v6 binds current publication to a successful/reused score job with the exact target date, formula hash, calendar hash, source-state hash, stock count and score-snapshot hash; prior score versions remain historical and provenance-bound.

The base score is a weighted blend:

```text
InstitutionalPersistence 35%
OwnershipAccumulation    35%
BrokerPersistence        30%
```

`LowPriceImpactFactor` can adjust the base only from `-10` to `+10`. It cannot create accumulation evidence when S-level flows are missing. All components are bounded, deterministic and stored in `accumulation_scores.components` with a human-readable `explanation`.

Institutional persistence includes 5D/10D/20D net values, positive-day counts/ratio, slope and `OneDaySpikeRatio = max(abs(daily net)) / sum(abs(daily net))`. Repeated positive days with a lower spike ratio outrank a single-day event. Foreign trading net and foreign actual holdings are separate datasets and separate features; actual foreign shares/ratio rising provides stronger ownership evidence but is not double-counted as institutional flow.

Holding levels are parsed by explicit unit patterns. Because FinMind buckets are mutually exclusive and cannot split a boundary bucket, the exposed metrics are `>400 lots` and `>1000 lots` (source lower bounds 400,001 and 1,000,001 shares), not unsupported exact `>=` claims. Row order is irrelevant. Under `finmind-holding-shares-level-v1`, every accepted observation must contain all 15 canonical thresholds exactly once with non-null percent, people and shares. Unknown, duplicate, missing or null buckets fail closed. A holiday-shifted observation within ±4 days is assigned to one Friday period; multiple source dates in one period are ambiguous and unavailable. Weekly gaps remain missing rather than zero.

Broker persistence uses directly observed, schema-valid positive-net rows in true 5/10/20-session windows. A broker branch is an execution venue aggregate and is never described as one beneficial owner. Omitted branches are `unknown`, never zero-filled; empty reports, missing sessions and unproven completeness make the affected window unavailable. Concentration and broker one-day-spike metrics remain unavailable until independent live evidence proves report completeness, so the current v6 score cannot claim those semantics.

Every required feature has an authoritative validation record: expected window, cadence, present, valid and reason. `5/5 sources` is only emitted for an individual stock when its score inputs are calculable; missing 19/20/21 sessions and unknown holding buckets remain `DATA_INSUFFICIENT` or `SCHEMA_MISMATCH`, never numeric zero. A globally partial source does not block unrelated ready stocks: the worker evaluates every eligible stock independently and records ready/not-ready counts in the score JobRun checkpoint. The exchange calendar is versioned as `tw-exchange-2026-v1`; dates outside its coverage fail closed as `CALENDAR_UNKNOWN`.

Classification:

```text
80–100 STRONG_ACCUMULATION
65–79.99 ACCUMULATION
50–64.99 WATCH
0–49.99 NO_STRONG_EVIDENCE
NULL DATA_INSUFFICIENT
```

## Capital-aware v7

`capital-aware-v7` is additive to `s-only-v6`; it is stored in
`capital_aware_scores`, so rebuilding v7 never rewrites a v6 row. The API
publishes both manifests and both SHA-256 formula hashes.

The provider's formal `Trading_money` and `Trading_Volume` fields produce
`DailyVWAP = Trading_money / Trading_Volume` only when both are positive.
`AverageTradingValue20D`, `MedianTradingValue20D`, `MedianVolume20D`,
`LowLiquidityDays20D` and `TradingValueStability20D` require a complete 20
session window; missing values remain unavailable. Estimated foreign,
investment-trust, dealer and total institutional net values multiply daily
institutional net shares by that day's formal VWAP. They are estimates, not
actual institutional execution costs.

`CapitalReference20D = max(positive EstimatedInstitutionalNetValue20D,
ConfirmedTop3BrokerNetBuyAmount20D)` rather than a sum, because these source
families can describe overlapping capital. Fixed TWD breakpoints are 0, 10m,
50m, 200m, 500m, 1bn and 5bn, mapped to 0, 15, 35, 55, 70, 85 and 100 for
`C`. `L = 80%` trading-value score plus `20%` volume score. `E` is 25 points
per independent source family, capped at 100. `LargeCapitalScore = 65%C +
20%L + 15% persistence`; `H = 30%S + 30%C + 25%L + 15%E - price penalty`.
An absolute reference below TWD 200m or a median daily trading value below
TWD 10m cannot enter a large-capital conclusion, even with a high ratio.

High-confidence eligibility requires S >= 50, median 20D trading value >=
TWD 50m, absolute reference >= TWD 200m, at least two independent source
families and a 20D return no greater than 30%. A return above 30% subtracts
20 H points and emits a gate warning without changing S. Statuses are
`HIGH_CONFIDENCE_ACCUMULATION`, `LARGE_CAPITAL_ACCUMULATION`,
`CAPITAL_WATCH`, `LIQUIDITY_TOO_LOW`, `CAPITAL_TOO_SMALL` and
`DATA_INSUFFICIENT`. Broker amount features use only validated positive rows;
unknown branches are not zero-filled and broker branches are not beneficial
owners.
