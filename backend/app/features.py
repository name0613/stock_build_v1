from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import math
from statistics import median, pstdev
from typing import Any

from .scoring import BROKER_CONFIRMATION_TWD, BROKER_ROW_CONTRACT_VERSION, HOLDING_SCHEMA_VERSION, INSTITUTIONAL_CONFIRMATION_TWD, MIN_CONFIRMATION_POSITIVE_DAYS, holding_period_anchor, holding_schema_state, one_day_spike_ratio, parse_holding_level, positive_day_ratio, rolling_sum, slope


def _ordered(rows: list[dict[str, Any]], date_key: str = "date") -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get(date_key) or row.get("source_date") or ""))


def institutional_features(rows: list[dict[str, Any]], prices: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
    # Trading_money is the provider's formal field.  A missing value makes the
    # corresponding estimated amount unavailable; it is never reconstructed
    # from close * volume.
    price_by_date = {
        str(row.get("date") or row.get("source_date") or "")[:10]: row
        for row in (prices or [])
    }
    for prefix, key in (("Foreign", "foreign_net"), ("InvestmentTrust", "investment_trust_net"), ("Dealer", "dealer_net"), ("Institutional", "institutional_net")):
        daily_values: list[float | None] = []
        for row in ordered:
            day = str(row.get("date") or row.get("source_date") or "")[:10]
            value = _finite_number(row.get(key))
            vwap = _daily_vwap(price_by_date.get(day))
            daily_values.append(value * vwap if value is not None and vwap is not None else None)
        if prefix == "Institutional":
            features["EstimatedInstitutionalNetValueSeries20D"] = [{"source_date": str(row.get("date") or row.get("source_date") or "")[:10], "value": value} for row, value in zip(ordered[-20:], daily_values[-20:])]
        for window in (5, 20):
            features[f"Estimated{prefix}NetValue{window}D"] = rolling_sum(daily_values, window)
    trading_values = [_positive_number(price_by_date.get(str(row.get("date") or row.get("source_date") or "")[:10], {}).get("trading_money")) for row in ordered]
    estimated_institutional = features.get("EstimatedInstitutionalNetValue20D")
    trading_total = sum(trading_values[-20:]) if len(trading_values) >= 20 and all(value is not None for value in trading_values[-20:]) else None
    features["InstitutionalNetToTradingValue20D"] = estimated_institutional / trading_total if estimated_institutional is not None and trading_total and trading_total > 0 else None
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
    # Exact duplicate provider events are ignored.  A single branch/day is
    # still one confirmation event even when the provider emits price-level
    # rows; this prevents a duplicated branch/day amount from inflating Top 3.
    unique_rows: list[dict[str, Any]] = []
    seen_events: set[tuple[Any, ...]] = set()
    for row in rows:
        event_key = (
            str(row.get("date") or row.get("source_date") or ""),
            str(row.get("securities_trader_id") or "unknown"),
            row.get("buy_volume"), row.get("sell_volume"), row.get("net_volume"),
            row.get("buy_amount"), row.get("sell_amount"),
        )
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        unique_rows.append(row)
    rows = unique_rows
    dates = sorted({str(row.get("date") or row.get("source_date") or "") for row in rows})
    broker_daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    broker_amount_daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    amount_complete = True
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
        buy_amount = _finite_number(row.get("buy_amount"))
        sell_amount = _finite_number(row.get("sell_amount"))
        if buy_amount is None or sell_amount is None:
            amount_complete = False
        else:
            broker_amount_daily[broker][day] += buy_amount - sell_amount
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
    amount_ranked: list[tuple[str, float, int]] = []
    if amount_complete:
        for broker, daily in broker_amount_daily.items():
            positive_days = sum(1 for day in last_dates if daily.get(day, 0.0) > 0)
            total_amount = sum(max(0.0, daily.get(day, 0.0)) for day in last_dates)
            if positive_days >= MIN_CONFIRMATION_POSITIVE_DAYS and total_amount > 0:
                amount_ranked.append((broker, total_amount, positive_days))
        amount_ranked.sort(key=lambda x: (-x[1], x[0]))
    confirmed_positive_amount = sum(item[1] for item in amount_ranked)
    top_broker_amount = amount_ranked[0][1] if amount_ranked else None
    top3_broker_amount = sum(item[1] for item in amount_ranked[:3]) if amount_ranked else None
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
        "ConfirmedTopBrokerNetBuyAmount20D": top_broker_amount,
        "ConfirmedTop3BrokerNetBuyAmount20D": top3_broker_amount,
        "ConfirmedPositiveBrokerAmount20D": confirmed_positive_amount if amount_ranked else None,
        "BrokerAmountPersistence20D": (sum(item[2] for item in amount_ranked) / (len(amount_ranked) * 20)) if amount_ranked else None,
        "BrokerAmountDataAvailable": amount_complete and bool(amount_ranked),
        "BrokerDataContract": {"available": True, "reason": None, "omitted_branch_policy": "unknown_not_zero", "report_completeness_required": False},
    }


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _broker_unavailable(reason: str) -> dict[str, Any]:
    return {"BrokerPersistenceScore": None, "BrokerOneDaySpikeRatio20D": None, "ConfirmedTopBrokerNetBuyAmount20D": None, "ConfirmedTop3BrokerNetBuyAmount20D": None, "ConfirmedPositiveBrokerAmount20D": None, "BrokerAmountPersistence20D": None, "BrokerAmountDataAvailable": False, "BrokerDataContract": {"available": False, "reason": reason}}


def _concentration(ranked: list[tuple[str, int, float]], count: int, total: float) -> float | None:
    if total <= 0:
        return None
    return max(0.0, min(1.0, sum(max(x[2], 0.0) for x in ranked[:count]) / total))


def price_features(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = _ordered(rows)
    closes = [row.get("close") for row in ordered]
    volumes = [row.get("volume") for row in ordered]
    trading_values = [row.get("trading_money") for row in ordered]
    out: dict[str, Any] = {}
    for window in (5, 10, 20):
        out[f"PriceReturn{window}D"] = _return(closes, window)
    if len(volumes) >= 20 and all(v is not None for v in volumes[-20:]):
        out["AverageVolume20D"] = sum(volumes[-20:]) / 20
        out["MedianVolume20D"] = float(median(volumes[-20:]))
    else:
        out["AverageVolume20D"] = None
        out["MedianVolume20D"] = None
    valid_trading_values = [_positive_number(value) for value in trading_values[-20:]] if len(trading_values) >= 20 else []
    if len(valid_trading_values) == 20 and all(value is not None for value in valid_trading_values):
        numeric_values = [float(value) for value in valid_trading_values]
        out["TradingValue1D"] = numeric_values[-1]
        out["AverageTradingValue20D"] = sum(numeric_values) / 20
        out["MedianTradingValue20D"] = float(median(numeric_values))
        out["LowLiquidityDays20D"] = sum(value < 10_000_000 for value in numeric_values)
        average = out["AverageTradingValue20D"]
        out["TradingValueStability20D"] = max(0.0, min(1.0, 1.0 - pstdev(numeric_values) / average)) if average else None
    else:
        out["TradingValue1D"] = _positive_number(trading_values[-1]) if trading_values else None
        out["AverageTradingValue20D"] = None
        out["MedianTradingValue20D"] = None
        out["LowLiquidityDays20D"] = None
        out["TradingValueStability20D"] = None
    out["TradingValueSeries20D"] = [{"source_date": str(row.get("date") or row.get("source_date") or "")[:10], "value": _positive_number(row.get("trading_money"))} for row in ordered[-20:]]
    latest_price = ordered[-1] if ordered else None
    out["DailyVWAP"] = _daily_vwap(latest_price)
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


def _positive_number(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _daily_vwap(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    money = _positive_number(row.get("trading_money"))
    volume = _positive_number(row.get("volume"))
    return money / volume if money is not None and volume is not None else None


def _return(values: list[float | None], window: int) -> float | None:
    if len(values) < window + 1 or values[-1] is None or values[-window - 1] in (None, 0):
        return None
    return float(values[-1] / values[-window - 1] - 1)


def build_features(institutional: list[dict[str, Any]], foreign: list[dict[str, Any]], holdings: list[dict[str, Any]], brokers: list[dict[str, Any]], prices: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    result.update(institutional_features(institutional, prices))
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
    result["CrossSourceConfirmation"] = cross_source_confirmation(result)
    return result


def cross_source_confirmation(features: dict[str, Any]) -> dict[str, Any]:
    """Build independent source-family confirmations without double counting."""
    families: list[dict[str, Any]] = []
    institutional_value = features.get("EstimatedInstitutionalNetValue20D")
    institutional_ratio = features.get("InstitutionalPositiveDayRatio20D")
    if institutional_value is not None and institutional_ratio is not None and institutional_value >= INSTITUTIONAL_CONFIRMATION_TWD and institutional_ratio >= 0.50:
        families.append({"family": "institutional_positive_net_value", "dataset": "TaiwanStockInstitutionalInvestorsBuySellWide", "reason": "20D 估算法人淨買金額達門檻且正買超日過半"})
    foreign_change = features.get("ForeignSharesChange20D")
    foreign_ratio_change = features.get("ForeignShareRatioChange20D")
    if (foreign_change is not None and foreign_change > 0) or (foreign_ratio_change is not None and foreign_ratio_change > 0):
        families.append({"family": "foreign_holding_increase", "dataset": "TaiwanStockShareholding", "reason": "外資實際持股股數或比例 20D 增加"})
    large_holder_change = features.get("LargeHolder400Change4W")
    if large_holder_change is not None and large_holder_change > 0:
        families.append({"family": "large_holder_400_increase", "dataset": "TaiwanStockHoldingSharesPer", "reason": ">400 張持股比例 4W 增加"})
    broker_amount = features.get("ConfirmedTop3BrokerNetBuyAmount20D")
    broker_persistence = features.get("BrokerAmountPersistence20D")
    if broker_amount is not None and broker_persistence is not None and broker_amount >= BROKER_CONFIRMATION_TWD and broker_persistence >= 0.25:
        families.append({"family": "verified_broker_positive_amount", "dataset": "TaiwanStockTradingDailyReport", "reason": "已驗證分點正買金額達門檻且具 20D 持續性"})
    return {"independent_source_count": len(families), "families": families, "source_datasets": [item["dataset"] for item in families], "available": bool(families), "independence_policy": "one family per dataset; fields within a dataset are not separate sources"}
