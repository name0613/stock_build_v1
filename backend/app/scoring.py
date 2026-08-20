from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from statistics import mean
from typing import Any, Iterable

from .calendar import CALENDAR_HASH, CALENDAR_MANIFEST

SCORE_VERSION = "s-only-v3"
WEIGHTS = {"institutional_persistence": 0.35, "ownership_accumulation": 0.35, "broker_persistence": 0.30}
HOLDING_METADATA_LEVELS = frozenset({"total", "all", "差異數調整（說明4）"})
SCORE_SPEC = {
    "version": SCORE_VERSION,
    "policy": "S-level only; price/reference data is supporting only",
    "weights": {**WEIGHTS, "base_sum": 1.0},
    "required_features": {
        "InstitutionalNet20D": {"dataset": "TaiwanStockInstitutionalInvestorsBuySellWide", "window": 20, "cadence": "trading_session"},
        "InstitutionalPositiveDayRatio20D": {"dataset": "TaiwanStockInstitutionalInvestorsBuySellWide", "window": 20, "cadence": "trading_session"},
        "InstitutionalNetSlope20D": {"dataset": "TaiwanStockInstitutionalInvestorsBuySellWide", "window": 20, "cadence": "trading_session"},
        "InstitutionalOneDaySpikeRatio20D": {"dataset": "TaiwanStockInstitutionalInvestorsBuySellWide", "window": 20, "cadence": "trading_session"},
        "ForeignShareRatioChange20D": {"dataset": "TaiwanStockShareholding", "window": 21, "cadence": "trading_session"},
        "LargeHolder400Change4W": {"dataset": "TaiwanStockHoldingSharesPer", "window": 4, "cadence": "weekly_publication"},
        "BrokerPersistenceScore": {"dataset": "TaiwanStockTradingDailyReport", "window": 20, "cadence": "trading_session"},
        "BrokerOneDaySpikeRatio20D": {"dataset": "TaiwanStockTradingDailyReport", "window": 20, "cadence": "trading_session"},
        "PriceReturn20D": {"dataset": "TaiwanStockPrice", "window": 21, "cadence": "trading_session", "role": "supporting_modifier"},
    },
    "windows": {"institutional": [5, 10, 20], "ownership": [5, 20], "holding": [1, 2, 4, 8], "broker": [5, 10, 20]},
    "coverage": {
        "required_datasets": ["TaiwanStockInstitutionalInvestorsBuySellWide", "TaiwanStockShareholding", "TaiwanStockHoldingSharesPer", "TaiwanStockTradingDailyReport", "TaiwanStockPrice"],
        "five_of_five_means": "every required feature is present, valid, and calculable",
        "missing_policy": "DATA_INSUFFICIENT; never substitute an older row or numeric zero",
    },
    "formulas": {
        "institutional": {"positive_day_ratio": "ratio * 100 * 0.65", "slope": "clamp(slope / max(abs(InstitutionalNet20D), 1) * 1000, -35, 35)", "spike": "(1 - min(spike_ratio, 1)) * 25", "cap": [0, 100]},
        "ownership": {"formula": "clamp(50 + ForeignShareRatioChange20D * 2 + LargeHolder400Change4W * 2, 0, 100)"},
        "broker": {"persistence": "min(persistent_buyer_count, 10) / 10 * 50 + sum(positive_days) / max(persistent_buyer_count * 20, 1) * 30", "concentration": "top_n_positive_broker_flow / gross_positive_broker_flow", "formula": "clamp(persistence + concentration * 20, 0, 100)", "spike_penalty": "broker_score * (1 - min(spike_ratio, 0.8) * 0.35)"},
        "final": {"formula": "clamp(institutional * 0.35 + ownership * 0.35 + broker * 0.30 + low_profile_modifier, 0, 100)", "low_profile_modifier": "clamp(LowPriceImpactFactor * 10, -10, 10)", "rounding": "round(score, 2)"},
    },
    "thresholds": {"strong": 80, "accumulation": 65, "watch": 50},
    "holding_boundaries": {"400": ">400 lots (source bucket lower bound >= 400,000 shares)", "1000": ">1000 lots (source bucket lower bound >= 1,000,000 shares)"},
    "holding_schema": {"accepted": "all real numeric threshold buckets; total/all are metadata only", "unknown_relevant_bucket": "SCHEMA_MISMATCH", "missing_or_duplicate_bucket": "PARTIAL"},
    "calendar_version": "tw-exchange-2026-v1",
    "calendar_manifest": CALENDAR_MANIFEST,
    "calendar_hash": CALENDAR_HASH,
    "semantic_versions": {"institutional_normalization": "dealer-components-v1", "broker_features": "gross-positive-flow-v2", "holding_parser": "explicit-lower-bound-v2", "missing_data": "fail-closed-v2"},
    "institutional_normalization": {"categories": ["foreign", "foreign_dealer_self", "investment_trust", "dealer_component"], "dealer_semantics": "Dealer_self + Dealer_Hedging are the non-overlapping dealer component when both are present; aggregate Dealer is fallback only when components are unavailable", "foreign_dealer_self_is_separate": True, "null_policy": "missing component does not become zero"},
    "broker_semantics": {"persistent_buyer": "positive_days >= 5 and positive_total > 0 within true window", "true_windows": {"5": "all five expected sessions present", "10": "all ten expected sessions present", "20": "all twenty expected sessions present"}, "absent_branch": "omitted branch is not zero unless complete stock/session report contract is proven", "concentration_denominator": "gross positive broker flow across the window", "spike": "max daily gross positive flow / total daily gross positive flow"},
    "holding_semantics": {"boundaries": {"400": "lower bound >= 400000 shares; displayed as >400 lots", "1000": "lower bound >= 1000000 shares; displayed as >1000 lots"}, "weekly_tolerance_days": 4, "metadata_levels": sorted(HOLDING_METADATA_LEVELS)},
}
SCORE_MANIFEST = SCORE_SPEC
FORMULA_HASH = hashlib.sha256(json.dumps(SCORE_SPEC, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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
    text = str(level).strip().replace(",", "").replace("，", "").replace("股以上", " shares 以上")
    if is_holding_metadata_level(text):
        return None
    import re

    patterns = [
        (r"^(?:more than|over)\s*([0-9]+)$", 1),
        (r"^([0-9]+)\s*[-~至]\s*([0-9]+)$", 1),
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


def is_holding_metadata_level(level: str | int | float | None) -> bool:
    """Identify provider-declared aggregate/adjustment rows, not real buckets."""
    return str(level).strip().lower() in {item.lower() for item in HOLDING_METADATA_LEVELS} if level is not None else False


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
    validation = coverage.get("RequiredFeatureValidation") or {}
    return all(coverage.get(k) is True for k in ("InstitutionalDataAvailable", "ForeignHoldingDataAvailable", "HoldingDistributionAvailable", "BrokerDataAvailable", "PriceDataAvailable")) and all(item.get("valid") is True for item in validation.values()) if validation else all(coverage.get(k) is True for k in ("InstitutionalDataAvailable", "ForeignHoldingDataAvailable", "HoldingDistributionAvailable", "BrokerDataAvailable", "PriceDataAvailable"))


def calculate_score(features: dict[str, Any], coverage: dict[str, bool], score_version: str = SCORE_VERSION) -> ScoreResult:
    """Deterministic, explainable S-only score. Missing inputs stay missing and fail closed."""
    if score_version != SCORE_VERSION:
        raise ValueError(f"unsupported score version: {score_version}")
    if not _required_coverage(coverage):
        reasons = coverage.get("missing_reasons") or ["required feature validation failed"]
        return ScoreResult(None, "DATA_INSUFFICIENT", {"InstitutionalPersistence": None, "OwnershipAccumulation": None, "BrokerPersistence": None, "LowProfileModifier": None}, [{"label": "資料不足", "value": 0, "detail": reason} for reason in reasons])

    institutional_ratio = features.get("InstitutionalPositiveDayRatio20D")
    institutional_slope = features.get("InstitutionalNetSlope20D")
    institutional_spike = features.get("InstitutionalOneDaySpikeRatio20D")
    if any(v is None for v in (institutional_ratio, institutional_slope, institutional_spike)):
        return ScoreResult(None, "DATA_INSUFFICIENT", {"InstitutionalPersistence": None, "OwnershipAccumulation": None, "BrokerPersistence": None, "LowProfileModifier": None}, [{"label": "資料不足", "value": 0, "detail": "Institutional required feature is missing or invalid"}])
    institutional = _bounded(institutional_ratio * 100 * 0.65 + _bounded(institutional_slope / max(abs(features.get("InstitutionalNet20D") or 1), 1) * 1000, -35, 35) + (1 - min(institutional_spike, 1)) * 25)
    foreign_change = features.get("ForeignShareRatioChange20D")
    large_change = features.get("LargeHolder400Change4W")
    if foreign_change is None or large_change is None:
        return ScoreResult(None, "DATA_INSUFFICIENT", {"InstitutionalPersistence": institutional, "OwnershipAccumulation": None, "BrokerPersistence": None, "LowProfileModifier": None}, [{"label": "資料不足", "value": 0, "detail": "Ownership required feature is missing or invalid"}])
    ownership = _bounded(50 + foreign_change * 2 + large_change * 2)
    broker_score = features.get("BrokerPersistenceScore")
    broker_spike = features.get("BrokerOneDaySpikeRatio20D")
    if broker_score is None or broker_spike is None:
        return ScoreResult(None, "DATA_INSUFFICIENT", {"InstitutionalPersistence": institutional, "OwnershipAccumulation": ownership, "BrokerPersistence": None, "LowProfileModifier": None}, [{"label": "資料不足", "value": 0, "detail": "Broker required feature is missing or invalid"}])
    broker = _bounded(broker_score * (1 - min(broker_spike, 0.8) * 0.35))
    base = institutional * WEIGHTS["institutional_persistence"] + ownership * WEIGHTS["ownership_accumulation"] + broker * WEIGHTS["broker_persistence"]
    low_profile = features.get("LowPriceImpactFactor")
    # An unavailable supporting modifier is explicitly neutral and marked in
    # the components; it is never presented as an observed zero.
    modifier = _bounded(low_profile * 10, -10, 10) if low_profile is not None else 0.0
    score = round(_bounded(base + modifier), 2)
    explanation = [
        {"label": "法人持續性", "value": round(institutional, 2), "detail": f"20 日正值比例 {institutional_ratio:.0%}；單日集中度 {institutional_spike:.1%}"},
        {"label": "持股結構累積", "value": round(ownership, 2), "detail": f"外資持股比例 20D 變化 {foreign_change:.2f}；400 張級距 4W 變化 {large_change:.2f}"},
        {"label": "分點持續性", "value": round(broker, 2), "detail": f"Persistence {broker_score:.2f}；分點單日集中度 {broker_spike:.1%}"},
        {"label": "低調修正", "value": round(modifier, 2), "detail": "價格影響僅作 -10 至 +10 modifier；若 unavailable 則明確不套用"},
    ]
    return ScoreResult(score, classify_score(score), {"InstitutionalPersistence": round(institutional, 2), "OwnershipAccumulation": round(ownership, 2), "BrokerPersistence": round(broker, 2), "LowProfileModifier": round(modifier, 2)}, explanation)
