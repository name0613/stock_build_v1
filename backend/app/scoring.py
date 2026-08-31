from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import hashlib
import json
import math
from statistics import mean
from typing import Any, Iterable

from .calendar import CALENDAR_HASH, CALENDAR_MANIFEST

SCORE_VERSION = "s-only-v6"
WEIGHTS = {"institutional_persistence": 0.35, "ownership_accumulation": 0.35, "broker_persistence": 0.30}
HOLDING_METADATA_LEVELS = frozenset({"total", "all", "差異數調整（說明4）"})
HOLDING_SCHEMA_VERSION = "finmind-holding-shares-level-v1"
HOLDING_WEEKLY_PERIOD_VERSION = "friday-anchor-nearest-v1"
HOLDING_WEEKLY_TOLERANCE_DAYS = 4
HOLDING_CANONICAL_LEVELS = (
    ("1-999", 1),
    ("1,000-5,000", 1_000),
    ("5,001-10,000", 5_001),
    ("10,001-15,000", 10_001),
    ("15,001-20,000", 15_001),
    ("20,001-30,000", 20_001),
    ("30,001-40,000", 30_001),
    ("40,001-50,000", 40_001),
    ("50,001-100,000", 50_001),
    ("100,001-200,000", 100_001),
    ("200,001-400,000", 200_001),
    ("400,001-600,000", 400_001),
    ("600,001-800,000", 600_001),
    ("800,001-1,000,000", 800_001),
    ("more than 1,000,001", 1_000_001),
)
HOLDING_CANONICAL_THRESHOLDS = frozenset(threshold for _, threshold in HOLDING_CANONICAL_LEVELS)
HOLDING_RELEVANT_THRESHOLDS = frozenset({400_001, 600_001, 800_001, 1_000_001})
BROKER_ROW_CONTRACT_VERSION = "finmind-observed-stock-session-row-v1"
# Kept as an import alias for older evidence helpers. It now identifies only
# observed-row validity and must never be interpreted as report completeness.
BROKER_REPORT_CONTRACT_VERSION = BROKER_ROW_CONTRACT_VERSION
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
        "broker": {"confirmed_positive_events": "only provider rows whose stock, session, branch, buy and sell fields validate", "persistence": "min(confirmed_persistent_buyer_count, 10) / 10 * 70 + confirmed_positive_days / max(confirmed_persistent_buyer_count * 20, 1) * 30", "formula": "clamp(persistence, 0, 100)", "omitted_rows": "unknown and ignored; never imputed as zero", "concentration_and_spike": "not scored because omitted-row completeness is not independently proven"},
        "final": {"formula": "clamp(institutional * 0.35 + ownership * 0.35 + broker * 0.30 + low_profile_modifier, 0, 100)", "low_profile_modifier": "clamp(LowPriceImpactFactor * 10, -10, 10)", "rounding": "round(score, 2)"},
    },
    "thresholds": {"strong": 80, "accumulation": 65, "watch": 50},
    "holding_boundaries": {"400": ">400 lots (source bucket lower bound >= 400,001 shares)", "1000": ">1000 lots (source bucket lower bound >= 1,000,001 shares)"},
    "holding_schema": {"version": HOLDING_SCHEMA_VERSION, "canonical_levels": [{"label": label, "threshold": threshold} for label, threshold in HOLDING_CANONICAL_LEVELS], "required_relevant_thresholds": sorted(HOLDING_RELEVANT_THRESHOLDS), "metadata_levels": sorted(HOLDING_METADATA_LEVELS), "unknown_bucket": "SCHEMA_MISMATCH", "missing_duplicate_or_null_canonical_bucket": "DATA_INSUFFICIENT"},
    "calendar_version": "tw-exchange-2026-v1",
    "calendar_manifest": CALENDAR_MANIFEST,
    "calendar_hash": CALENDAR_HASH,
    "semantic_versions": {"institutional_normalization": "dealer-components-v1", "broker_features": "confirmed-positive-events-v6-no-omission-imputation", "holding_parser": HOLDING_SCHEMA_VERSION, "holding_weekly_period": HOLDING_WEEKLY_PERIOD_VERSION, "missing_data": "fail-closed-v6-current-score-run-gate"},
    "institutional_normalization": {"categories": ["foreign", "foreign_dealer_self", "investment_trust", "dealer_component"], "dealer_semantics": "Dealer_self + Dealer_Hedging are the non-overlapping dealer component when both are present; aggregate Dealer is fallback only when components are unavailable", "foreign_dealer_self_is_separate": True, "null_policy": "missing component does not become zero"},
    "broker_semantics": {"provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION, "persistent_buyer": "at least 5 directly observed positive-net sessions in the 20-session window", "session_coverage": "at least one schema-valid provider row must exist for every expected session", "absent_branch": "unknown; ignored and never represented as zero", "partial_report_safety": "omissions can only remove confirmed positive events and therefore cannot create a positive broker signal", "concentration": "not calculated", "spike": "not calculated"},
    "holding_semantics": {"boundaries": {"400": "lower bound >= 400001 shares; displayed as >400 lots", "1000": "lower bound >= 1000001 shares; displayed as >1000 lots"}, "weekly_period_version": HOLDING_WEEKLY_PERIOD_VERSION, "weekly_anchor": "Friday", "weekly_tolerance_days": HOLDING_WEEKLY_TOLERANCE_DAYS, "one_observation_per_period": True, "metadata_levels": sorted(HOLDING_METADATA_LEVELS)},
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


def holding_period_anchor(observation_date: date, expected_periods: Iterable[date] | None = None) -> date | None:
    """Assign one observation to one Friday-anchored publication period."""
    if expected_periods is None:
        previous_friday = observation_date - timedelta(days=(observation_date.weekday() - 4) % 7)
        next_friday = previous_friday + timedelta(days=7)
        candidates = (previous_friday, next_friday)
    else:
        candidates = tuple(expected_periods)
    ranked = sorted((abs((candidate - observation_date).days), candidate) for candidate in candidates)
    if not ranked or ranked[0][0] > HOLDING_WEEKLY_TOLERANCE_DAYS:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def holding_schema_state(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Validate the exact versioned FinMind HoldingSharesLevel vocabulary."""
    buckets: dict[int, list[dict[str, Any]]] = {}
    unknown_levels: list[str] = []
    for row in rows:
        level = row.get("HoldingSharesLevel") or row.get("holding_shares_level")
        if is_holding_metadata_level(level):
            continue
        threshold = row.get("holding_shares_threshold")
        if threshold is None:
            threshold = parse_holding_level(level)
        if threshold is None or int(threshold) not in HOLDING_CANONICAL_THRESHOLDS:
            unknown_levels.append(str(level))
            continue
        buckets.setdefault(int(threshold), []).append(row)
    observed = set(buckets)
    relevant = observed & HOLDING_RELEVANT_THRESHOLDS
    missing = sorted(HOLDING_CANONICAL_THRESHOLDS - observed)
    duplicates = sorted(threshold for threshold, bucket_rows in buckets.items() if len(bucket_rows) != 1)
    invalid_fields = sorted({
        threshold
        for threshold in HOLDING_CANONICAL_THRESHOLDS
        for row in buckets.get(threshold, [])
        if any(row.get(field) is None for field in ("percent", "people", "shares"))
    })
    reasons: list[str] = []
    if missing:
        reasons.append("missing_required_relevant_bucket")
    if duplicates:
        reasons.append("duplicate_normalized_bucket")
    if invalid_fields:
        reasons.append("null_percent_people_or_shares")
    if unknown_levels:
        reasons.append("unknown_holding_bucket")
    return {
        "available": not reasons,
        "schema_version": HOLDING_SCHEMA_VERSION,
        "expected_canonical_thresholds": sorted(HOLDING_CANONICAL_THRESHOLDS),
        "observed_canonical_thresholds": sorted(observed),
        "expected_relevant_thresholds": sorted(HOLDING_RELEVANT_THRESHOLDS),
        "observed_relevant_thresholds": sorted(relevant),
        "missing_thresholds": missing,
        "duplicate_thresholds": duplicates,
        "invalid_thresholds": invalid_fields,
        "unknown_levels": sorted(set(unknown_levels)),
        "reasons": reasons,
    }


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
    if broker_score is None:
        return ScoreResult(None, "DATA_INSUFFICIENT", {"InstitutionalPersistence": institutional, "OwnershipAccumulation": ownership, "BrokerPersistence": None, "LowProfileModifier": None}, [{"label": "資料不足", "value": 0, "detail": "Broker required feature is missing or invalid"}])
    broker = _bounded(broker_score)
    base = institutional * WEIGHTS["institutional_persistence"] + ownership * WEIGHTS["ownership_accumulation"] + broker * WEIGHTS["broker_persistence"]
    low_profile = features.get("LowPriceImpactFactor")
    # An unavailable supporting modifier is explicitly neutral and marked in
    # the components; it is never presented as an observed zero.
    modifier = _bounded(low_profile * 10, -10, 10) if low_profile is not None else 0.0
    score = round(_bounded(base + modifier), 2)
    explanation = [
        {"label": "法人持續性", "value": round(institutional, 2), "detail": f"20 日正值比例 {institutional_ratio:.0%}；單日集中度 {institutional_spike:.1%}"},
        {"label": "持股結構累積", "value": round(ownership, 2), "detail": f"外資持股比例 20D 變化 {foreign_change:.2f}；400 張級距 4W 變化 {large_change:.2f}"},
        {"label": "分點持續性", "value": round(broker, 2), "detail": f"已驗證正買超事件 Persistence {broker_score:.2f}；缺漏分點保持 unknown、不補零"},
        {"label": "低調修正", "value": round(modifier, 2), "detail": "價格影響僅作 -10 至 +10 modifier；若 unavailable 則明確不套用"},
    ]
    return ScoreResult(score, classify_score(score), {"InstitutionalPersistence": round(institutional, 2), "OwnershipAccumulation": round(ownership, 2), "BrokerPersistence": round(broker, 2), "LowProfileModifier": round(modifier, 2)}, explanation)


# v7 is deliberately additive.  The v6 constants and calculate_score above
# are the historical stealth contract and must not be changed in place.
CAPITAL_AWARE_SCORE_VERSION = "capital-aware-v7"
CAPITAL_TWD_KNOTS = (0.0, 10_000_000.0, 50_000_000.0, 200_000_000.0, 500_000_000.0, 1_000_000_000.0, 5_000_000_000.0)
CAPITAL_SCORE_KNOTS = (0.0, 15.0, 35.0, 55.0, 70.0, 85.0, 100.0)
LIQUIDITY_TWD_KNOTS = (0.0, 10_000_000.0, 50_000_000.0, 200_000_000.0, 1_000_000_000.0, 5_000_000_000.0)
LIQUIDITY_SCORE_KNOTS = (0.0, 20.0, 45.0, 70.0, 90.0, 100.0)
VOLUME_KNOTS = (0.0, 100_000.0, 500_000.0, 2_000_000.0, 10_000_000.0)
VOLUME_SCORE_KNOTS = (0.0, 20.0, 45.0, 70.0, 100.0)
MIN_LIQUIDITY_TWD_20D = 50_000_000.0
MIN_CAPITAL_TWD_20D = 200_000_000.0
LOW_LIQUIDITY_TWD_20D = 10_000_000.0
PRICE_REFLECTED_RETURN_20D = 0.30
INSTITUTIONAL_CONFIRMATION_TWD = 100_000_000.0
BROKER_CONFIRMATION_TWD = 50_000_000.0
MIN_CONFIRMATION_POSITIVE_DAYS = 5

CAPITAL_AWARE_SCORE_SPEC = {
    "version": CAPITAL_AWARE_SCORE_VERSION,
    "policy": "capital scale is absolute TWD first; ratios are supporting evidence only",
    "outputs": ["StealthAccumulationScore", "LiquidityScore", "CapitalScaleScore", "ConfirmationScore", "LargeCapitalScore", "HighConfidenceScore"],
    "weights": {"large_capital": {"capital": 0.65, "liquidity": 0.20, "persistence": 0.15}, "high_confidence": {"stealth": 0.30, "capital": 0.30, "liquidity": 0.25, "confirmation": 0.15}},
    "breakpoints_twd": {"capital": list(CAPITAL_TWD_KNOTS), "capital_scores": list(CAPITAL_SCORE_KNOTS), "liquidity": list(LIQUIDITY_TWD_KNOTS), "liquidity_scores": list(LIQUIDITY_SCORE_KNOTS), "volume_shares": list(VOLUME_KNOTS), "volume_scores": list(VOLUME_SCORE_KNOTS)},
    "thresholds": {"minimum_median_trading_value_20d_twd": MIN_LIQUIDITY_TWD_20D, "minimum_capital_reference_20d_twd": MIN_CAPITAL_TWD_20D, "low_liquidity_median_trading_value_20d_twd": LOW_LIQUIDITY_TWD_20D, "minimum_confirmation_sources": 2, "minimum_stealth_score": 50, "price_reflected_return_20d": PRICE_REFLECTED_RETURN_20D, "institutional_confirmation_twd": INSTITUTIONAL_CONFIRMATION_TWD, "broker_confirmation_twd": BROKER_CONFIRMATION_TWD, "minimum_confirmation_positive_days": MIN_CONFIRMATION_POSITIVE_DAYS},
    "windows": {"liquidity": 20, "capital": [5, 20], "confirmation": 20, "price": 20},
    "missing_policy": "Trading_money, Trading_Volume, VWAP or required 20D institutional value remains NULL; no zero substitution",
    "overlap_policy": "CapitalReference20D is max(positive estimated institutional net value, confirmed top-3 broker net buy amount), never a sum of overlapping sources",
    "confirmation_families": ["institutional_positive_net_value", "foreign_holding_increase", "large_holder_400_increase", "verified_broker_positive_amount"],
    "broker_policy": "only provider-row-contract-valid positive events; omitted branches unknown; exact duplicate normalized events ignored; no complete-market concentration claim",
    "price_policy": "PriceReturn20D above 30% subtracts 20 points from H and fails the high-confidence gate; S is unchanged",
    "status_policy": ["HIGH_CONFIDENCE_ACCUMULATION", "LARGE_CAPITAL_ACCUMULATION", "CAPITAL_WATCH", "LIQUIDITY_TOO_LOW", "CAPITAL_TOO_SMALL", "DATA_INSUFFICIENT"],
    "formula": "C=piecewise(CapitalReference20D); L=0.8*piecewise(MedianTradingValue20D)+0.2*piecewise(MedianVolume20D); E=min(100, confirmations/4*100); P=100*mean(InstitutionalPositiveDayRatio20D, BrokerAmountPersistence20D when available); Large=0.65C+0.20L+0.15P; H=0.30S+0.30C+0.25L+0.15E-price_penalty",
    "formula_version": "capital-aware-v7-piecewise-fixed-twd-v1",
    "source_semantics": {"estimated_institutional_value": "daily institutional net shares multiplied by formal DailyVWAP; estimated, not actual execution cash flow", "broker_amount": "verified positive broker-row events only; broker branch is not a beneficial owner"},
}
CAPITAL_AWARE_SCORE_MANIFEST = CAPITAL_AWARE_SCORE_SPEC
CAPITAL_AWARE_FORMULA_HASH = hashlib.sha256(json.dumps(CAPITAL_AWARE_SCORE_SPEC, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class CapitalAwareScoreResult:
    score: float | None
    status: str
    components: dict[str, Any] = field(default_factory=dict)
    explanation: list[dict[str, Any]] = field(default_factory=list)
    eligible: bool = False
    eligibility_reasons: list[str] = field(default_factory=list)
    large_capital_score: float | None = None


def _piecewise(value: float, x_knots: tuple[float, ...], y_knots: tuple[float, ...]) -> float:
    if value <= x_knots[0]:
        return y_knots[0]
    for left, right, left_score, right_score in zip(x_knots, x_knots[1:], y_knots, y_knots[1:]):
        if value <= right:
            fraction = (value - left) / (right - left)
            return left_score + fraction * (right_score - left_score)
    return y_knots[-1]


def _capital_reference(features: dict[str, Any]) -> float | None:
    values = []
    for name in ("EstimatedInstitutionalNetValue20D", "ConfirmedTop3BrokerNetBuyAmount20D"):
        value = features.get(name)
        if value is not None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                values.append(max(0.0, number))
    return max(values) if values else None


def calculate_capital_aware_score(features: dict[str, Any], coverage: dict[str, Any], stealth_score: float | None = None) -> CapitalAwareScoreResult:
    """Calculate v7 without changing the historical v6 score.

    A score can be numerically calculated for a partial gate state, but its
    status remains an explicit gate outcome.  Missing formal trading value or
    a required 20-session value is DATA_INSUFFICIENT, never zero-filled.
    """
    if stealth_score is None:
        raw_stealth = features.get("StealthAccumulationScore")
        stealth_score = float(raw_stealth) if raw_stealth is not None else None
    required = {"MedianTradingValue20D": features.get("MedianTradingValue20D"), "AverageTradingValue20D": features.get("AverageTradingValue20D"), "MedianVolume20D": features.get("MedianVolume20D")}
    reference = _capital_reference(features)
    missing = [name for name, value in required.items() if value is None]
    if reference is None:
        missing.append("CapitalReference20D")
    if missing:
        return CapitalAwareScoreResult(None, "DATA_INSUFFICIENT", {"StealthAccumulationScore": stealth_score, "LiquidityScore": None, "CapitalScaleScore": None, "ConfirmationScore": None, "LargeCapitalScore": None, "HighConfidenceScore": None, "CapitalReference20D": None, "eligibility_reasons": [f"missing_{name}" for name in missing]}, [{"label": "資料不足", "value": None, "detail": "正式 Trading_money／Trading_Volume 或 20 日估算法人金額不足；不補 0。"}], False, [f"missing_{name}" for name in missing], None)

    median_value = float(features["MedianTradingValue20D"])
    median_volume = float(features["MedianVolume20D"])
    liquidity = _bounded(0.8 * _piecewise(median_value, LIQUIDITY_TWD_KNOTS, LIQUIDITY_SCORE_KNOTS) + 0.2 * _piecewise(median_volume, VOLUME_KNOTS, VOLUME_SCORE_KNOTS))
    capital = _piecewise(float(reference), CAPITAL_TWD_KNOTS, CAPITAL_SCORE_KNOTS)
    confirmation = features.get("CrossSourceConfirmation") or {}
    confirmation_count = int(confirmation.get("independent_source_count", confirmation.get("count", 0)) or 0)
    confirmation_score = _bounded(min(4, confirmation_count) / 4 * 100)
    institutional_ratio = features.get("InstitutionalPositiveDayRatio20D")
    broker_persistence = features.get("BrokerAmountPersistence20D")
    persistence_values = [float(value) for value in (institutional_ratio, broker_persistence) if value is not None]
    persistence = _bounded(sum(persistence_values) / len(persistence_values) * 100) if persistence_values else None
    if persistence is None:
        return CapitalAwareScoreResult(None, "DATA_INSUFFICIENT", {"StealthAccumulationScore": stealth_score, "LiquidityScore": round(liquidity, 2), "CapitalScaleScore": round(capital, 2), "ConfirmationScore": round(confirmation_score, 2), "LargeCapitalScore": None, "HighConfidenceScore": None}, [{"label": "資料不足", "value": None, "detail": "持續性窗口不足；不把缺值當成零。"}], False, ["missing_persistence_20d"], None)

    large = _bounded(capital * 0.65 + liquidity * 0.20 + persistence * 0.15)
    price_return = features.get("PriceReturn20D")
    penalty = 20.0 if price_return is not None and float(price_return) > PRICE_REFLECTED_RETURN_20D else (10.0 if price_return is not None and float(price_return) > 0.20 else 0.0)
    high = _bounded((float(stealth_score) if stealth_score is not None else 0.0) * 0.30 + capital * 0.30 + liquidity * 0.25 + confirmation_score * 0.15 - penalty)
    reasons: list[str] = []
    if median_value < LOW_LIQUIDITY_TWD_20D:
        reasons.append("median_trading_value_below_10m_twd")
    if median_value < MIN_LIQUIDITY_TWD_20D:
        reasons.append("median_trading_value_below_50m_twd")
    if reference < MIN_CAPITAL_TWD_20D:
        reasons.append("capital_reference_below_200m_twd")
    if stealth_score is None:
        reasons.append("stealth_score_unavailable")
    elif stealth_score < 50:
        reasons.append("stealth_score_below_50")
    if confirmation_count < 2:
        reasons.append("fewer_than_two_independent_sources")
    if price_return is not None and float(price_return) > PRICE_REFLECTED_RETURN_20D:
        reasons.append("price_already_reflected_20d_return_above_30pct")
    eligible = not reasons
    if "median_trading_value_below_10m_twd" in reasons:
        status = "LIQUIDITY_TOO_LOW"
    elif "capital_reference_below_200m_twd" in reasons:
        status = "CAPITAL_TOO_SMALL"
    elif eligible:
        status = "HIGH_CONFIDENCE_ACCUMULATION"
    elif large >= 60 and median_value >= LOW_LIQUIDITY_TWD_20D:
        status = "LARGE_CAPITAL_ACCUMULATION"
    else:
        status = "CAPITAL_WATCH"
    components = {"StealthAccumulationScore": round(stealth_score, 2) if stealth_score is not None else None, "LiquidityScore": round(liquidity, 2), "CapitalScaleScore": round(capital, 2), "ConfirmationScore": round(confirmation_score, 2), "LargeCapitalScore": round(large, 2), "HighConfidenceScore": round(high, 2), "CapitalReference20D": round(reference, 2), "MedianTradingValue20D": round(median_value, 2), "PricePenalty": round(penalty, 2), "ConfirmationSourceCount": confirmation_count, "eligibility_reasons": reasons}
    explanation = [{"label": "絕對資金規模 C", "value": round(capital, 2), "detail": f"20D 保守資金參考值約新台幣 {reference:,.0f} 元；取估算法人金額與已確認前三分點金額較大者，不相加。"}, {"label": "流動性 L", "value": round(liquidity, 2), "detail": f"20D 中位日成交金額新台幣 {median_value:,.0f} 元；成交金額權重 80%，成交量權重 20%。"}, {"label": "獨立確認 E", "value": round(confirmation_score, 2), "detail": f"{confirmation_count} 個獨立來源家族確認；同一資料集欄位不重複算來源。"}, {"label": "高可信 H", "value": round(high, 2), "detail": "估算法人金額不是實際成交成本；分點是營業據點彙總，不是單一受益所有人。"}]
    return CapitalAwareScoreResult(round(high, 2), status, components, explanation, eligible, reasons, round(large, 2))
