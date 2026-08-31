from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.features import build_features
from app.ingestion import ingest_records, normalize_price
from app.models import Base, PriceDaily, SourceRevision, Stock
from app.scoring import CAPITAL_AWARE_FORMULA_HASH, CAPITAL_AWARE_SCORE_VERSION, calculate_capital_aware_score


def _capital_features(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "EstimatedInstitutionalNetValue20D": 500_000_000.0,
        "MedianTradingValue20D": 100_000_000.0,
        "AverageTradingValue20D": 100_000_000.0,
        "MedianVolume20D": 1_000_000.0,
        "InstitutionalPositiveDayRatio20D": 0.8,
        "BrokerAmountPersistence20D": 0.5,
        "CrossSourceConfirmation": {"independent_source_count": 3, "families": []},
        "PriceReturn20D": 0.10,
    }
    result.update(overrides)
    return result


def test_price_normalization_keeps_provider_money_and_never_derives_it() -> None:
    normalized = normalize_price({"stock_id": "2330", "date": "2026-08-20", "close": 100, "Trading_Volume": 1000})
    assert normalized is not None
    assert normalized["trading_money"] is None
    assert normalized["trading_turnover"] is None
    formal = normalize_price({"stock_id": "2330", "date": "2026-08-20", "close": 100, "Trading_Volume": 1000, "Trading_money": 123456, "Trading_turnover": 789})
    assert formal is not None
    assert formal["trading_money"] == 123456.0
    assert formal["trading_turnover"] == 789.0


def test_formal_money_features_have_vwap_windows_and_missing_policy() -> None:
    start = date(2026, 7, 24)
    institutional = [{"date": (start + timedelta(days=i)).isoformat(), "foreign_net": 1000, "investment_trust_net": 500, "dealer_net": 100, "institutional_net": 1600} for i in range(20)]
    prices = [{"date": (start + timedelta(days=i)).isoformat(), "close": 100, "volume": 1_000_000, "trading_money": 100_000_000, "trading_turnover": 10_000} for i in range(20)]
    result = build_features(institutional, [], [], [], prices)
    assert result["DailyVWAP"] == 100.0
    assert result["TradingValue1D"] == 100_000_000.0
    assert result["AverageTradingValue20D"] == 100_000_000.0
    assert result["MedianTradingValue20D"] == 100_000_000.0
    assert result["EstimatedInstitutionalNetValue20D"] == 3_200_000.0
    assert result["InstitutionalNetToTradingValue20D"] == 0.0016
    prices[-1]["trading_money"] = None
    missing = build_features(institutional, [], [], [], prices)
    assert missing["AverageTradingValue20D"] is None
    assert missing["MedianTradingValue20D"] is None
    assert missing["EstimatedInstitutionalNetValue20D"] is None


def test_capital_score_is_monotonic_and_gates_small_absolute_amount() -> None:
    high = calculate_capital_aware_score(_capital_features(), {"PriceDataAvailable": True}, stealth_score=80)
    larger = calculate_capital_aware_score(_capital_features(EstimatedInstitutionalNetValue20D=1_000_000_000.0), {}, stealth_score=80)
    less_liquid = calculate_capital_aware_score(_capital_features(MedianTradingValue20D=50_000_000.0, AverageTradingValue20D=50_000_000.0), {}, stealth_score=80)
    small_but_high_ratio = calculate_capital_aware_score(_capital_features(EstimatedInstitutionalNetValue20D=5_000_000.0, MedianTradingValue20D=100_000_000.0), {}, stealth_score=95)
    assert high.status == "HIGH_CONFIDENCE_ACCUMULATION"
    assert larger.components["CapitalScaleScore"] >= high.components["CapitalScaleScore"]
    assert less_liquid.components["LiquidityScore"] < high.components["LiquidityScore"]
    assert small_but_high_ratio.status == "CAPITAL_TOO_SMALL"
    assert small_but_high_ratio.status != "HIGH_CONFIDENCE_ACCUMULATION"
    assert high.components["CapitalReference20D"] == 500_000_000.0


def test_capital_score_requires_two_independent_sources_and_missing_money_is_not_zero() -> None:
    one_source = calculate_capital_aware_score(_capital_features(CrossSourceConfirmation={"independent_source_count": 1}), {}, stealth_score=80)
    no_money = calculate_capital_aware_score({"MedianTradingValue20D": None, "AverageTradingValue20D": None, "MedianVolume20D": 1_000_000.0}, {}, stealth_score=80)
    assert one_source.status in {"LARGE_CAPITAL_ACCUMULATION", "CAPITAL_WATCH"}
    assert "fewer_than_two_independent_sources" in one_source.eligibility_reasons
    assert no_money.status == "DATA_INSUFFICIENT"
    assert no_money.score is None


def test_capital_score_persisted_separately_from_v6_source_rows() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Stock(stock_id="2330", stock_name="台積電", market="上市", is_common_stock=True))
        db.commit()
        count = ingest_records(db, "TaiwanStockPrice", [{"stock_id": "2330", "date": "2026-08-20", "close": 100, "Trading_Volume": 1000, "Trading_money": 1000000, "Trading_turnover": 100}])
        row = db.scalar(select(PriceDaily).where(PriceDaily.stock_id == "2330"))
        revision = db.scalar(select(SourceRevision).where(SourceRevision.dataset == "TaiwanStockPrice"))
        assert count == 1
        assert row is not None and row.trading_money == 1000000.0 and row.trading_turnover == 100.0
        assert revision is not None and revision.payload["trading_money"] == 1000000.0
    assert CAPITAL_AWARE_SCORE_VERSION == "capital-aware-v7"
    assert len(CAPITAL_AWARE_FORMULA_HASH) == 64
