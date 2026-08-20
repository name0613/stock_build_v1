# Scoring

`score_version = s-only-v2`; the backend publishes the complete machine-readable formula specification at `/api/score-spec` and hashes every constant, transform, cap, threshold, window and missing-data rule into SHA-256 `formula_hash`. A material formula or source-policy change requires a new score version.

The base score is a weighted blend:

```text
InstitutionalPersistence 35%
OwnershipAccumulation    35%
BrokerPersistence        30%
```

`LowPriceImpactFactor` can adjust the base only from `-10` to `+10`. It cannot create accumulation evidence when S-level flows are missing. All components are bounded, deterministic and stored in `accumulation_scores.components` with a human-readable `explanation`.

Institutional persistence includes 5D/10D/20D net values, positive-day counts/ratio, slope and `OneDaySpikeRatio = max(abs(daily net)) / sum(abs(daily net))`. Repeated positive days with a lower spike ratio outrank a single-day event. Foreign trading net and foreign actual holdings are separate datasets and separate features; actual foreign shares/ratio rising provides stronger ownership evidence but is not double-counted as institutional flow.

Holding levels are parsed by explicit unit patterns. Because FinMind buckets are mutually exclusive and cannot split a boundary bucket, the exposed metrics are `>400 lots` and `>1000 lots` (lower bound at least 400,000/1,000,000 shares), not unsupported exact `>=` claims. Unknown formats fail validation. Row order is irrelevant. Weekly gaps remain missing rather than zero.

Broker persistence measures true 5/10/20-session windows, persistent buyer count, bounded gross-positive-flow concentration and one-day spike ratio. Concentration is part of the versioned persistence score and uses gross positive flow rather than signed market net, so offsetting sellers cannot create an impossible ratio. A broker branch is an execution venue aggregate and is never described as one beneficial owner.

Every required feature has an authoritative validation record: expected window, cadence, present, valid and reason. `5/5 sources` is only emitted when the score inputs are calculable; missing 19/20/21 sessions and unknown holding buckets remain `DATA_INSUFFICIENT` or `SCHEMA_MISMATCH`, never numeric zero. The exchange calendar is versioned as `tw-exchange-2026-v1`; dates outside its coverage fail closed as `CALENDAR_UNKNOWN`.

Classification:

```text
80–100 STRONG_ACCUMULATION
65–79.99 ACCUMULATION
50–64.99 WATCH
0–49.99 NO_STRONG_EVIDENCE
NULL DATA_INSUFFICIENT
```
