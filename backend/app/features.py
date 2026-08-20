from __future__ import annotations

from collections import defaultdict
from typing import Any

from .scoring import one_day_spike_ratio, parse_holding_level, positive_day_ratio, rolling_sum, slope


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
    """Aggregate by parsed lower-bound threshold; does not rely on API row order."""
    by_date: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        threshold = row.get("holding_shares_threshold") or parse_holding_level(row.get("holding_shares_level"))
        if threshold is None:
            continue
        key = str(row.get("date") or row.get("source_date") or "")
        by_date[key][threshold] = row
    ordered_dates = sorted(by_date)

    def metric(threshold: int, field: str) -> list[float | None]:
        return [by_date[d].get(threshold, {}).get(field) for d in ordered_dates]

    out: dict[str, Any] = {}
    for threshold, label in ((400_000, "400"), (1_000_000, "1000")):
        percent = metric(threshold, "percent")
        shares = metric(threshold, "shares")
        people = metric(threshold, "people")
        out[f"LargeHolder{label}LotsPercent"] = percent[-1] if percent else None
        out[f"LargeHolder{label}LotsShares"] = shares[-1] if shares else None
        out[f"LargeHolder{label}LotsPeople"] = people[-1] if people else None
        for weeks in (1, 2, 4, 8):
            out[f"LargeHolder{label}Change{weeks}W"] = _difference(percent, weeks * 1) if len(percent) >= weeks + 1 else None
    out["HoldingDistributionLatestDate"] = ordered_dates[-1] if ordered_dates else None
    return out


def broker_features(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"BrokerPersistenceScore": None, "BrokerOneDaySpikeRatio20D": None}
    dates = sorted({str(row.get("date") or row.get("source_date") or "") for row in rows})
    daily_totals: dict[str, float] = defaultdict(float)
    broker_daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        day = str(row.get("date") or row.get("source_date") or "")
        net = row.get("net_volume")
        if net is None:
            buy = row.get("buy_volume")
            sell = row.get("sell_volume")
            net = buy - sell if buy is not None and sell is not None else None
        if net is None:
            continue
        broker = str(row.get("securities_trader_id") or "unknown")
        broker_daily[broker][day] += float(net)
        daily_totals[day] += float(net)
    if len(dates) < 20 or any(day not in daily_totals for day in dates[-20:]):
        return {"BrokerPersistenceScore": None, "BrokerOneDaySpikeRatio20D": None}
    last_dates = dates[-20:]
    positive_brokers = []
    for broker, daily in broker_daily.items():
        positive_days = sum(1 for day in last_dates if daily.get(day, 0) > 0)
        total = sum(daily.get(day, 0) for day in last_dates)
        if positive_days >= 5 and total > 0:
            positive_brokers.append((broker, positive_days, total))
    total_abs = sum(abs(daily_totals[day]) for day in last_dates)
    spike = max((abs(daily_totals[day]) for day in last_dates), default=0) / total_abs if total_abs else 0.0
    persistent_count = len(positive_brokers)
    score = min(100.0, (min(persistent_count, 10) / 10) * 60 + (sum(x[1] for x in positive_brokers) / max(persistent_count * 20, 1)) * 40)
    ranked = sorted(positive_brokers, key=lambda x: x[2], reverse=True)
    net20 = sum(daily_totals[day] for day in last_dates)
    return {
        "TopBrokerNetBuy20D": ranked[0][2] if ranked else None,
        "Top3BrokerNet20D": sum(x[2] for x in ranked[:3]),
        "Top5BrokerNet20D": sum(x[2] for x in ranked[:5]),
        "Top10BrokerNet20D": sum(x[2] for x in ranked[:10]),
        "Top3BrokerConcentration20D": _concentration(ranked, 3, net20),
        "Top5BrokerConcentration20D": _concentration(ranked, 5, net20),
        "PersistentBuyerCount5D": sum(1 for _, days, _ in positive_brokers if days >= 5),
        "PersistentBuyerCount10D": sum(1 for _, days, _ in positive_brokers if days >= 10),
        "PersistentBuyerCount20D": sum(1 for _, days, _ in positive_brokers if days >= 15),
        "TopBrokerPositiveDays20D": ranked[0][1] if ranked else None,
        "BrokerPersistenceScore": score,
        "BrokerOneDaySpikeRatio20D": spike,
    }


def _concentration(ranked: list[tuple[str, int, float]], count: int, total: float) -> float | None:
    if total == 0:
        return 0.0
    return sum(x[2] for x in ranked[:count]) / total


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
    for row in prices:
        price_row = dict(row)
        price_row["broker_net"] = broker_daily.get(str(row.get("date") or row.get("source_date") or ""))
        price_rows.append(price_row)
    result.update(broker_features(brokers))
    result.update(price_features(price_rows))
    return result
