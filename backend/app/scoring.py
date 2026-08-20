from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Any, Iterable

SCORE_VERSION = "s-only-v1"
WEIGHTS = {"institutional_persistence": 0.35, "ownership_accumulation": 0.35, "broker_persistence": 0.30}


@dataclass(frozen=True)
class ScoreResult:
    score: float | None
    status: str
    components: dict[str, float | None]
    explanation: list[dict[str, Any]] = field(default_factory=list)


def net(buy: float | None, sell: float | None) -> float | None:
    if buy is None or sell is None:
        return None
    return float(buy) - float(sell)


def rolling_sum(values: Iterable[float | None], window: int) -> float | None:
    values = list(values)
    if len(values) < window or any(v is None for v in values[-window:]):
        return None
    return float(sum(values[-window:]))


def positive_day_ratio(values: Iterable[float | None], window: int) -> float | None:
    values = list(values)
    if len(values) < window or any(v is None for v in values[-window:]):
        return None
    return sum(1 for value in values[-window:] if value > 0) / window


def one_day_spike_ratio(values: Iterable[float | None], window: int = 20) -> float | None:
    values = list(values)
    if len(values) < window or any(v is None for v in values[-window:]):
        return None
    absolute = [abs(v) for v in values[-window:]]
    total = sum(absolute)
    return max(absolute) / total if total else 0.0


def slope(values: Iterable[float | None], window: int) -> float | None:
    values = list(values)
    if len(values) < window or any(v is None for v in values[-window:]):
        return None
    ys = values[-window:]
    x_mean = (window - 1) / 2
    y_mean = mean(ys)
    denominator = sum((i - x_mean) ** 2 for i in range(window))
    return sum((i - x_mean) * (y - y_mean) for i, y in enumerate(ys)) / denominator


def parse_holding_level(level: str | int | float | None) -> int | None:
    """Return the lower bound in shares; never relies on row ordering or loose contains."""
    if level is None:
        return None
    text = str(level).strip().replace(",", "").replace("，", "")
    import re

    patterns = [
        (r"^([0-9]+)\s*張\s*以上$", 1000),
        (r"^([0-9]+)\s*張\s*以上.*$", 1000),
        (r"^([0-9]+)\s*shares?\s*以上$", 1),
        (r"^([0-9]+)\s*股\s*以上$", 1),
        (r"^([0-9]+)\s*以上$", 1),
        (r"^([0-9]+)\s*[-~至]\s*([0-9]+)\s*張$", 1000),
    ]
    for index, (pattern, multiplier) in enumerate(patterns):
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1)) * multiplier
    return None


def _bounded(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def classify_score(score: float | None) -> str:
    if score is None:
        return "DATA_INSUFFICIENT"
    if score >= 80:
        return "STRONG_ACCUMULATION"
    if score >= 65:
        return "ACCUMULATION"
    if score >= 50:
        return "WATCH"
    return "NO_STRONG_EVIDENCE"


def _required_coverage(coverage: dict[str, bool]) -> bool:
    return all(coverage.get(k) is True for k in ("InstitutionalDataAvailable", "ForeignHoldingDataAvailable", "HoldingDistributionAvailable", "BrokerDataAvailable", "PriceDataAvailable"))


def calculate_score(features: dict[str, Any], coverage: dict[str, bool], score_version: str = SCORE_VERSION) -> ScoreResult:
    """Deterministic, explainable S-only score. Missing inputs stay missing and fail closed."""
    if score_version != SCORE_VERSION:
        raise ValueError(f"unsupported score version: {score_version}")
    if not _required_coverage(coverage):
        return ScoreResult(None, "DATA_INSUFFICIENT", {"InstitutionalPersistence": None, "OwnershipAccumulation": None, "BrokerPersistence": None, "LowProfileModifier": None}, [])

    institutional_ratio = features.get("InstitutionalPositiveDayRatio20D")
    institutional_slope = features.get("InstitutionalNetSlope20D")
    institutional_spike = features.get("InstitutionalOneDaySpikeRatio20D")
    if any(v is None for v in (institutional_ratio, institutional_slope, institutional_spike)):
        return ScoreResult(None, "DATA_INSUFFICIENT", {"InstitutionalPersistence": None, "OwnershipAccumulation": None, "BrokerPersistence": None, "LowProfileModifier": None}, [])
    institutional = _bounded(institutional_ratio * 100 * 0.65 + _bounded(institutional_slope / max(abs(features.get("InstitutionalNet20D") or 1), 1) * 1000, -35, 35) + (1 - min(institutional_spike, 1)) * 25)
    foreign_change = features.get("ForeignShareRatioChange20D")
    large_change = features.get("LargeHolder400Change4W")
    if foreign_change is None or large_change is None:
        return ScoreResult(None, "DATA_INSUFFICIENT", {"InstitutionalPersistence": institutional, "OwnershipAccumulation": None, "BrokerPersistence": None, "LowProfileModifier": None}, [])
    ownership = _bounded(50 + foreign_change * 2 + large_change * 2)
    broker_score = features.get("BrokerPersistenceScore")
    broker_spike = features.get("BrokerOneDaySpikeRatio20D")
    if broker_score is None or broker_spike is None:
        return ScoreResult(None, "DATA_INSUFFICIENT", {"InstitutionalPersistence": institutional, "OwnershipAccumulation": ownership, "BrokerPersistence": None, "LowProfileModifier": None}, [])
    broker = _bounded(broker_score * (1 - min(broker_spike, 0.8) * 0.35))
    base = institutional * WEIGHTS["institutional_persistence"] + ownership * WEIGHTS["ownership_accumulation"] + broker * WEIGHTS["broker_persistence"]
    low_profile = features.get("LowPriceImpactFactor")
    modifier = _bounded((low_profile or 0.0) * 10, -10, 10)
    score = round(_bounded(base + modifier), 2)
    explanation = [
        {"label": "法人持續性", "value": round(institutional, 2), "detail": f"20 日正值比例 {institutional_ratio:.0%}；單日集中度 {institutional_spike:.1%}"},
        {"label": "持股結構累積", "value": round(ownership, 2), "detail": f"外資持股比例 20D 變化 {foreign_change:.2f}；400 張級距 4W 變化 {large_change:.2f}"},
        {"label": "分點持續性", "value": round(broker, 2), "detail": f"Persistence {broker_score:.2f}；分點單日集中度 {broker_spike:.1%}"},
        {"label": "低調修正", "value": round(modifier, 2), "detail": "價格影響僅作 -10 至 +10 modifier，不獨立產生建倉證據"},
    ]
    return ScoreResult(score, classify_score(score), {"InstitutionalPersistence": round(institutional, 2), "OwnershipAccumulation": round(ownership, 2), "BrokerPersistence": round(broker, 2), "LowProfileModifier": round(modifier, 2)}, explanation)

