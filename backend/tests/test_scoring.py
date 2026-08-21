from __future__ import annotations

import pytest

from app.features import broker_features, holding_distribution_features, institutional_features
from app.scoring import BROKER_REPORT_CONTRACT_VERSION, HOLDING_CANONICAL_LEVELS, classify_score, calculate_score, holding_schema_state, is_holding_metadata_level, one_day_spike_ratio, parse_holding_level, positive_day_ratio, rolling_sum


def _holding_rows(day: str, relevant_percent: dict[int, float]) -> list[dict[str, object]]:
    return [
        {"date": day, "holding_shares_level": level, "percent": relevant_percent.get(threshold, 0.0), "people": 1, "shares": threshold}
        for level, threshold in HOLDING_CANONICAL_LEVELS
    ]


def test_holding_level_parser_supports_explicit_units_and_rejects_ambiguous() -> None:
    assert parse_holding_level("400張以上") == 400_000
    assert parse_holding_level("1,000 張以上") == 1_000_000
    assert parse_holding_level("400000股以上") == 400_000
    assert parse_holding_level("400 shares 以上") == 400
    assert parse_holding_level("大於400") is None
    assert is_holding_metadata_level("差異數調整（說明4）")
    assert parse_holding_level("差異數調整（說明4）") is None


def test_holding_aggregation_does_not_depend_on_row_order() -> None:
    first = {400_001: 20.0, 600_001: 15.0, 800_001: 10.0, 1_000_001: 5.0}
    second = {400_001: 22.0, 600_001: 16.0, 800_001: 11.0, 1_000_001: 6.0}
    rows = list(reversed(_holding_rows("2026-08-14", second) + _holding_rows("2026-08-07", first)))
    result = holding_distribution_features(rows)
    assert result["LargeHolder400LotsPercent"] == 55.0
    assert result["LargeHolder400LotsPeople"] == 4
    assert result["LargeHolder400Change1W"] == 5.0
    assert result["LargeHolder1000LotsPercent"] == 6.0


@pytest.mark.parametrize("missing_threshold", [threshold for _, threshold in HOLDING_CANONICAL_LEVELS])
def test_holding_schema_requires_every_canonical_bucket(missing_threshold: int) -> None:
    rows = [row for row in _holding_rows("2026-08-14", {}) if parse_holding_level(str(row["holding_shares_level"])) != missing_threshold]
    state = holding_schema_state(rows)
    assert state["available"] is False
    assert missing_threshold in state["missing_thresholds"]


def test_holding_schema_rejects_duplicate_unknown_and_null_and_ignores_row_order() -> None:
    rows = _holding_rows("2026-08-14", {})
    assert holding_schema_state(list(reversed(rows)))["available"] is True
    duplicate = rows + [dict(rows[-1])]
    assert holding_schema_state(duplicate)["duplicate_thresholds"] == [1_000_001]
    unknown = rows + [{"holding_shares_level": "future provider bucket", "percent": 1, "people": 1, "shares": 1}]
    assert holding_schema_state(unknown)["unknown_levels"] == ["future provider bucket"]
    invalid = [dict(row) for row in rows]
    invalid[7]["people"] = None
    assert holding_schema_state(invalid)["invalid_thresholds"] == [40_001]


def test_institutional_net_rolling_and_persistence_metrics() -> None:
    rows = [{"date": f"2026-07-{i:02d}", "foreign_net": i, "investment_trust_net": 2, "dealer_net": -1, "institutional_net": i + 1} for i in range(1, 21)]
    result = institutional_features(rows)
    assert result["ForeignNet5D"] == 90
    assert result["ForeignPositiveDays20D"] == 20
    assert result["InstitutionalPositiveDayRatio20D"] == 1.0
    assert result["InstitutionalNetSlope20D"] > 0


def test_missing_rolling_values_stay_missing() -> None:
    assert rolling_sum([1, None, 3], 3) is None
    assert positive_day_ratio([1, None, 3], 3) is None
    assert one_day_spike_ratio([1, None, 3], 3) is None


def test_broker_persistence_rewards_repeated_buying_over_single_spike() -> None:
    persistent = []
    spiky = []
    for day in range(20):
        ds = f"2026-07-{day + 1:02d}"
        persistent.extend([{"date": ds, "securities_trader_id": "A", "net_volume": 100, "provider_report_complete": True, "provider_contract_version": BROKER_REPORT_CONTRACT_VERSION}, {"date": ds, "securities_trader_id": "B", "net_volume": 50, "provider_report_complete": True, "provider_contract_version": BROKER_REPORT_CONTRACT_VERSION}])
        spiky.extend([{"date": ds, "securities_trader_id": "A", "net_volume": 3000 if day == 19 else 0, "provider_report_complete": True, "provider_contract_version": BROKER_REPORT_CONTRACT_VERSION}, {"date": ds, "securities_trader_id": "B", "net_volume": 0, "provider_report_complete": True, "provider_contract_version": BROKER_REPORT_CONTRACT_VERSION}])
    persistent_score = broker_features(persistent)
    spiky_score = broker_features(spiky)
    assert persistent_score["BrokerPersistenceScore"] > spiky_score["BrokerPersistenceScore"]
    assert spiky_score["BrokerOneDaySpikeRatio20D"] > persistent_score["BrokerOneDaySpikeRatio20D"]


def full_features() -> dict[str, float]:
    return {"InstitutionalPositiveDayRatio20D": 0.8, "InstitutionalNetSlope20D": 100, "InstitutionalNet20D": 2000, "InstitutionalOneDaySpikeRatio20D": 0.2, "ForeignShareRatioChange20D": 1.2, "LargeHolder400Change4W": 1.5, "BrokerPersistenceScore": 75, "BrokerOneDaySpikeRatio20D": 0.15, "LowPriceImpactFactor": 0.3}


def full_coverage() -> dict[str, bool]:
    return {"InstitutionalDataAvailable": True, "ForeignHoldingDataAvailable": True, "HoldingDistributionAvailable": True, "BrokerDataAvailable": True, "PriceDataAvailable": True}


def test_score_is_versioned_transparent_and_classified() -> None:
    result = calculate_score(full_features(), full_coverage())
    assert result.score is not None
    assert 0 <= result.score <= 100
    assert result.status == classify_score(result.score)
    assert {"InstitutionalPersistence", "OwnershipAccumulation", "BrokerPersistence", "LowProfileModifier"} == set(result.components)
    assert len(result.explanation) == 4


def test_missing_s_level_data_fails_closed_not_zero() -> None:
    coverage = full_coverage()
    coverage["ForeignHoldingDataAvailable"] = False
    result = calculate_score(full_features(), coverage)
    assert result.score is None
    assert result.status == "DATA_INSUFFICIENT"


def test_historical_score_does_not_change_when_future_row_arrives() -> None:
    base = full_features()
    day20 = calculate_score(base, full_coverage())
    future = dict(base)
    future["InstitutionalPositiveDayRatio20D"] = 0.1
    future["InstitutionalOneDaySpikeRatio20D"] = 0.95
    future_score_as_day20 = calculate_score(base, full_coverage())
    day21_score = calculate_score(future, full_coverage())
    assert day20.score == future_score_as_day20.score
    assert day21_score.score != day20.score


def test_score_golden_vector_and_complete_spec_hash() -> None:
    from app.scoring import FORMULA_HASH, SCORE_MANIFEST
    import hashlib
    import json

    result = calculate_score(full_features(), full_coverage())
    assert result.score == 78.71
    mutated = dict(SCORE_MANIFEST)
    mutated["formulas"] = {**SCORE_MANIFEST["formulas"], "final": {**SCORE_MANIFEST["formulas"]["final"], "rounding": "round(score, 3)"}}
    mutated_hash = hashlib.sha256(json.dumps(mutated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert mutated_hash != FORMULA_HASH


def test_calendar_manifest_is_content_bound_and_provenance_visible() -> None:
    import hashlib
    import json

    from app.calendar import CALENDAR_HASH, CALENDAR_MANIFEST, calendar_snapshot
    canonical = json.dumps(CALENDAR_MANIFEST, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == CALENDAR_HASH
    assert calendar_snapshot()["calendar_hash"] == CALENDAR_HASH
    mutated = {**CALENDAR_MANIFEST, "holidays": [*CALENDAR_MANIFEST["holidays"], "2026-11-11"]}
    assert hashlib.sha256(json.dumps(mutated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != CALENDAR_HASH
