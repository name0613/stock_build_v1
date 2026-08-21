from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import math
from typing import Any

from .scoring import BROKER_ROW_CONTRACT_VERSION, HOLDING_SCHEMA_VERSION, holding_period_anchor, holding_schema_state, one_day_spike_ratio, parse_holding_level, positive_day_ratio, rolling_sum, slope


def _ordered(rows: list[dict[str, Any]], date_key: str = "date") -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get(date_key) or ""))


def institutional_features(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = _ordered(rows)
    keys = ("foreign_net", "investment_trust_net", "dealer_net", "institutional_net")
    values = {key: [row.get(key) for row in ordered] for key in keys}
    features: dict[str, Any] = {}
    for prefix, key in (("ForeignNet", "foreign_net"), ("InvestmentTrustNet", "investment_trust_net"), ("DealerNet", "dealer_net"), ("InstitutionalNet", "institutional_net")):
        for window in (1, 5, 10, 20):
            result = rolling_sum(values[key], window)
            features[f"{prefix}{window}D"] = result
    for window in (5, 10, 20):
        features[f"ForeignPositiveDays{window}D"] = sum(1 for v in values["foreign_net"][-window:] if v is not None and v > 0) if len(ordered) >= window and all(v is not None for v in values["foreign_net"][-window:]) else None
        features[f"InvestmentTrustPositiveDays{window}D"] = sum(1 for v in values["investment_trust_net"][-window:] if v is not None and v > 0) if len(ordered) >= window and all(v is not None for v in values["investment_trust_net"][-window:]) else None
    features["InvestmentTrustPositiveDays20D"] = features["InvestmentTrustPositiveDays20D"]
    features["InstitutionalPositiveDayRatio20D"] = positive_day_ratio(values["institutional_net"], 20)
    features["InstitutionalNetSlope20D"] = slope(values["institutional_net"], 20)
    features["InstitutionalOneDaySpikeRatio20D"] = one_day_spike_ratio(values["institutional_net"], 20)
    return features


def foreign_holding_features(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = _ordered(rows)
    shares = [row.get("foreign_investment_shares") for row in ordered]
    ratios = [row.get("foreign_investment_shares_ratio") for row in ordered]
    out: dict[str, Any] = {}
    for window in (5, 10, 20):
        out[f"ForeignSharesChange{window}D"] = _difference(shares, window)
    for window in (5, 20):
        out[f"ForeignShareRatioChange{window}D"] = _difference(ratios, window)
    out["ForeignHoldingTrend20D"] = slope(ratios, 20)
    return out


def _difference(values: list[float | None], window: int) -> float | None:
    if len(values) < window + 1 or values[-1] is None or values[-window - 1] is None:
        return None
    return float(values[-1] - values[-window - 1])


def holding_distribution_features(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate mutually-exclusive FinMind buckets by lower-bound threshold.

    FinMind's level buckets cannot isolate the exact boundary row, so the
    public metric is ``>400``/``>1000 lots`` (all buckets whose lower bound is
    at least the threshold).  Weekly deltas require a real observation near
    the expected publication date; an arbitrary older row is never substituted.
    """
    by_period: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        source_day = _parse_date(str(row.get("date") or row.get("source_date") or ""))
        if source_day is None:
            continue
        anchor = holding_period_anchor(source_day)
        if anchor is None:
            continue
        by_period[anchor.isoformat()][source_day.isoformat()].append(row)
    ordered_periods = sorted(by_period)
    schema_by_period: dict[str, dict[str, Any]] = {}
    selected_rows: dict[str, list[dict[str, Any]]] = {}
    for period, candidates in by_period.items():
        if len(candidates) != 1:
            schema_by_period[period] = {"available": False, "schema_version": HOLDING_SCHEMA_VERSION, "candidate_source_dates": sorted(candidates), "reasons": ["multiple_observations_in_weekly_period"]}
            selected_rows[period] = []
            continue
        source_date, candidate_rows = next(iter(candidates.items()))
        schema_by_period[period] = {**holding_schema_state(candidate_rows), "source_date": source_date, "candidate_source_dates": [source_date]}
        selected_rows[period] = candidate_rows

    def metric(threshold: int, field: str) -> list[float | None]:
        values: list[float | None] = []
        for period in ordered_periods:
            if not schema_by_period[period]["available"]:
                values.append(None)
                continue
            selected = [row.get(field) for row in selected_rows[period] if (lower := (row.get("holding_shares_threshold") or parse_holding_level(row.get("holding_shares_level")))) is not None and lower >= threshold]
            values.append(sum(selected) if selected and all(value is not None for value in selected) else None)
        return values

    out: dict[str, Any] = {}
    series: dict[str, list[dict[str, Any]]] = {"400": [], "1000": []}
    for threshold, label in ((400_000, "400"), (1_000_000, "1000")):
        percent = metric(threshold, "percent")
        shares = metric(threshold, "shares")
        people = metric(threshold, "people")
        out[f"LargeHolder{label}LotsPercent"] = percent[-1] if percent else None
        out[f"LargeHolder{label}LotsShares"] = shares[-1] if shares else None
        out[f"LargeHolder{label}LotsPeople"] = people[-1] if people else None
        for weeks in (1, 2, 4, 8):
            out[f"LargeHolder{label}Change{weeks}W"] = _weekly_difference(percent, ordered_periods, weeks)
        series[label] = [{"source_date": schema_by_period[period].get("source_date") or period, "period_anchor": period, "value": value} for period, value in zip(ordered_periods, percent)]
    latest_period = ordered_periods[-1] if ordered_periods else None
    out["HoldingDistributionLatestDate"] = schema_by_period.get(latest_period, {}).get("source_date") if latest_period else None
    out["HoldingBoundarySemantics"] = {"400": ">400 lots", "1000": ">1000 lots"}
    out["HoldingDistributionSeries"] = series
    out["HoldingDistributionCoverage"] = {**_holding_coverage(ordered_periods), "schema_version": HOLDING_SCHEMA_VERSION, "weekly_period_version": "friday-anchor-nearest-v1", "by_period": schema_by_period, "available": bool(ordered_periods and schema_by_period[ordered_periods[-1]]["available"])}
    return out


def _weekly_difference(values: list[float | None], dates: list[str], weeks: int) -> float | None:
    if not dates or not values or values[-1] is None:
        return None
    latest = _parse_date(dates[-1])
    if latest is None:
        return None
    target = latest - timedelta(days=7 * weeks)
    candidates = [(abs((_parse_date(day) - target).days), value) for day, value in zip(dates, values) if _parse_date(day) is not None and value is not None]
    if not candidates:
        return None
    distance, previous = min(candidates, key=lambda item: item[0])
    return float(values[-1] - previous) if distance == 0 else None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def _holding_coverage(dates: list[str]) -> dict[str, Any]:
    if not dates:
        return {"available": False, "missing_weeks": []}
    parsed = [_parse_date(value) for value in dates]
    missing_weeks: list[int] = []
    for weeks in (1, 2, 4, 8):
        if parsed[-1] is None or not any(day is not None and day == parsed[-1] - timedelta(days=7 * weeks) for day in parsed[:-1]):
            missing_weeks.append(weeks)
    return {"available": True, "missing_weeks": missing_weeks}


def broker_features(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return _broker_unavailable("no_broker_rows")
    if any(row.get("provider_row_validated") is not True or row.get("provider_row_contract_version") != BROKER_ROW_CONTRACT_VERSION for row in rows):
        return _broker_unavailable("provider_row_contract_not_proven")
    dates = sorted({str(row.get("date") or row.get("source_date") or "") for row in rows})
    broker_daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        day = str(row.get("date") or row.get("source_date") or "")
        net = _finite_number(row.get("net_volume"))
        if net is None:
            buy = _finite_number(row.get("buy_volume"))
            sell = _finite_number(row.get("sell_volume"))
            net = buy - sell if buy is not None and sell is not None else None
        if net is None:
            return _broker_unavailable("null_or_invalid_broker_net")
        broker = str(row.get("securities_trader_id") or "unknown")
        broker_daily[broker][day] += float(net)
    if len(dates) < 20 or any(day not in {d for daily in broker_daily.values() for d in daily} for day in dates[-20:]):
        return _broker_unavailable("incomplete_provider_sessions")
    last_dates = dates[-20:]
    positive_brokers = []
    for broker, daily in broker_daily.items():
        observed_positive = [daily[day] for day in last_dates if day in daily and daily[day] > 0]
        positive_days = len(observed_positive)
        if positive_days >= 5:
            positive_brokers.append((broker, positive_days, sum(observed_positive)))
    persistent_count = len(positive_brokers)
    score = (min(persistent_count, 10) / 10) * 70 + (sum(x[1] for x in positive_brokers) / max(persistent_count * 20, 1)) * 30
    ranked = sorted(positive_brokers, key=lambda x: x[2], reverse=True)
    true_window_counts: dict[int, int | None] = {}
    for window in (5, 10, 20):
        window_dates = last_dates[-window:]
        true_window_counts[window] = sum(1 for _, daily in broker_daily.items() if sum(1 for day in window_dates if day in daily and daily[day] > 0) >= max(1, window // 2))
    return {
        "TopBrokerNetBuy20D": ranked[0][2] if ranked else None,
        "Top3BrokerNet20D": sum(x[2] for x in ranked[:3]),
        "Top5BrokerNet20D": sum(x[2] for x in ranked[:5]),
        "Top10BrokerNet20D": sum(x[2] for x in ranked[:10]),
        "Top3BrokerConcentration20D": None,
        "Top5BrokerConcentration20D": None,
        "BrokerConcentrationDenominator20D": None,
        "PersistentBuyerCount5D": true_window_counts[5],
        "PersistentBuyerCount10D": true_window_counts[10],
        "PersistentBuyerCount20D": true_window_counts[20],
        "TopBrokerPositiveDays20D": ranked[0][1] if ranked else None,
        "BrokerPersistenceScore": min(100.0, score),
        "BrokerOneDaySpikeRatio20D": None,
        "BrokerPositiveFlowSpikeRatio20D": None,
        "BrokerDataContract": {"available": True, "reason": None, "omitted_branch_policy": "unknown_not_zero", "report_completeness_required": False},
    }


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _broker_unavailable(reason: str) -> dict[str, Any]:
    return {"BrokerPersistenceScore": None, "BrokerOneDaySpikeRatio20D": None, "BrokerDataContract": {"available": False, "reason": reason}}


def _concentration(ranked: list[tuple[str, int, float]], count: int, total: float) -> float | None:
    if total <= 0:
        return None
    return max(0.0, min(1.0, sum(max(x[2], 0.0) for x in ranked[:count]) / total))


def price_features(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = _ordered(rows)
    closes = [row.get("close") for row in ordered]
    volumes = [row.get("volume") for row in ordered]
    out: dict[str, Any] = {}
    for window in (5, 10, 20):
        out[f"PriceReturn{window}D"] = _return(closes, window)
    if len(volumes) >= 20 and all(v is not None for v in volumes[-20:]):
        out["AverageVolume20D"] = sum(volumes[-20:]) / 20
    else:
        out["AverageVolume20D"] = None
    institutional_net = rolling_sum([row.get("institutional_net") for row in ordered], 20)
    broker_net = rolling_sum([row.get("broker_net") for row in ordered], 20)
    avg_volume = out["AverageVolume20D"]
    out["InstitutionalNetToVolume20D"] = institutional_net / (avg_volume * 20) if institutional_net is not None and avg_volume else None
    out["BrokerNetToVolume20D"] = broker_net / (avg_volume * 20) if broker_net is not None and avg_volume else None
    price_return = out.get("PriceReturn20D")
    flow = out.get("InstitutionalNetToVolume20D")
    if price_return is None or flow is None:
        out["LowPriceImpactFactor"] = None
    elif flow > 0:
        out["LowPriceImpactFactor"] = max(-1.0, min(1.0, 0.5 - price_return * 5))
    else:
        out["LowPriceImpactFactor"] = 0.0
    return out


def _return(values: list[float | None], window: int) -> float | None:
    if len(values) < window + 1 or values[-1] is None or values[-window - 1] in (None, 0):
        return None
    return float(values[-1] / values[-window - 1] - 1)


def build_features(institutional: list[dict[str, Any]], foreign: list[dict[str, Any]], holdings: list[dict[str, Any]], brokers: list[dict[str, Any]], prices: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    result.update(institutional_features(institutional))
    result.update(foreign_holding_features(foreign))
    result.update(holding_distribution_features(holdings))
    broker_daily = defaultdict(float)
    for row in brokers:
        key = str(row.get("date") or row.get("source_date") or "")
        if row.get("net_volume") is not None:
            broker_daily[key] += row["net_volume"]
    price_rows = []
    institutional_by_date = {str(row.get("date") or row.get("source_date") or ""): row.get("institutional_net") for row in institutional}
    for row in prices:
        price_row = dict(row)
        price_row["institutional_net"] = institutional_by_date.get(str(row.get("date") or row.get("source_date") or ""))
        price_row["broker_net"] = broker_daily.get(str(row.get("date") or row.get("source_date") or ""))
        price_rows.append(price_row)
    result.update(broker_features(brokers))
    result.update(price_features(price_rows))
    return result
