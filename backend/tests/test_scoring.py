from __future__ import annotations

from app.features import broker_features, holding_distribution_features, institutional_features
from app.scoring import classify_score, calculate_score, one_day_spike_ratio, parse_holding_level, positive_day_ratio, rolling_sum


def test_holding_level_parser_supports_explicit_units_and_rejects_ambiguous() -> None:
    assert parse_holding_level("400張以上") == 400_000
    assert parse_holding_level("1,000 張以上") == 1_000_000
    assert parse_holding_level("400000股以上") == 400_000
    assert parse_holding_level("400 shares 以上") == 400
    assert parse_holding_level("大於400") is None


def test_holding_aggregation_does_not_depend_on_row_order() -> None:
    rows = [
        {"date": "2026-08-14", "holding_shares_level": "1000張以上", "percent": 35.0, "people": 80, "shares": 1_000_000},
        {"date": "2026-08-07", "holding_shares_level": "400張以上", "percent": 52.0, "people": 100, "shares": 2_000_000},
        {"date": "2026-08-14", "holding_shares_level": "400張以上", "percent": 55.0, "people": 99, "shares": 2_100_000},
        {"date": "2026-08-07", "holding_shares_level": "1000張以上", "percent": 33.0, "people": 81, "shares": 980_000},
    ]
    result = holding_distribution_features(rows)
    assert result["LargeHolder400LotsPercent"] == 55.0
    assert result["LargeHolder400LotsPeople"] == 99
    assert result["LargeHolder400Change1W"] == 3.0
    assert result["LargeHolder1000LotsPercent"] == 35.0


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
        persistent.extend([{"date": ds, "securities_trader_id": "A", "net_volume": 100}, {"date": ds, "securities_trader_id": "B", "net_volume": 50}])
        spiky.extend([{"date": ds, "securities_trader_id": "A", "net_volume": 3000 if day == 19 else 0}, {"date": ds, "securities_trader_id": "B", "net_volume": 0}])
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
