from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.calendar import CalendarUnknownError, expected_trading_sessions
from app.finmind import FinMindClient, FinMindError
from app.ingestion import _model_rows, _natural_key, calculate_stock_features_and_score, ingest_records
from app.models import AccumulationScore, Base, BrokerDaily, InstitutionalDaily, SourceRevision, Stock
from app.scoring import parse_holding_level


def _db() -> tuple[Session, object]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine), engine


def test_raw_institutional_fallback_is_explicitly_rejected() -> None:
    client = FinMindClient()
    with pytest.raises(FinMindError) as exc:
        client.fetch("TaiwanStockInstitutionalInvestorsBuySell", persist_raw=False)
    assert exc.value.code == "DATASET_NOT_ALLOWLISTED"


def test_calendar_version_fails_closed_outside_known_coverage() -> None:
    with pytest.raises(CalendarUnknownError):
        expected_trading_sessions(date(2027, 1, 4), 1)


def test_holding_schema_unknown_duplicate_and_null_are_explicit() -> None:
    db, _ = _db()
    db.add(Stock(stock_id="2330", stock_name="Test", market="上市", security_type="股票", is_common_stock=True))
    db.commit()
    with pytest.raises(FinMindError) as unknown:
        ingest_records(db, "TaiwanStockHoldingSharesPer", [{"stock_id": "2330", "date": "2026-08-20", "HoldingSharesLevel": "mystery bucket", "percent": 1}])
    assert unknown.value.code == "SCHEMA_MISMATCH"
    assert ingest_records(db, "TaiwanStockHoldingSharesPer", [{"stock_id": "2330", "date": "2026-08-20", "HoldingSharesLevel": "差異數調整（說明4）", "percent": 1}]) == 0
    with pytest.raises(FinMindError) as duplicate:
        ingest_records(db, "TaiwanStockHoldingSharesPer", [{"stock_id": "2330", "date": "2026-08-20", "HoldingSharesLevel": "400,001-600,000", "percent": 1}, {"stock_id": "2330", "date": "2026-08-20", "HoldingSharesLevel": "400001-600000", "percent": 2}])
    assert duplicate.value.code == "SCHEMA_MISMATCH"
    assert ingest_records(db, "TaiwanStockHoldingSharesPer", [{"stock_id": "2330", "date": "2026-08-20", "HoldingSharesLevel": "400,001-600,000", "percent": None}]) == 1


def test_holding_real_bucket_boundaries_and_weekly_gap_fail_closed() -> None:
    assert parse_holding_level("400,001-600,000") == 400001
    assert parse_holding_level("1,000 張以上") == 1_000_000
    from app.features import holding_distribution_features

    rows = [
        {"source_date": "2026-08-20", "HoldingSharesLevel": "400,001-600,000", "holding_shares_threshold": 400001, "percent": 10.0, "people": 3},
        {"source_date": "2026-08-20", "HoldingSharesLevel": "1,000,001-2,000,000", "holding_shares_threshold": 1000001, "percent": 4.0, "people": 1},
        {"source_date": "2026-07-16", "HoldingSharesLevel": "400,001-600,000", "holding_shares_threshold": 400001, "percent": 8.0, "people": 2},
    ]
    result = holding_distribution_features(rows)
    assert result["LargeHolder400LotsPercent"] == 14.0
    assert result["LargeHolder1000LotsPercent"] == 4.0
    assert result["LargeHolder400Change4W"] is None
    assert result["HoldingBoundarySemantics"]["400"] == ">400 lots"


def test_broker_concentration_is_bounded_with_offsetting_sellers() -> None:
    from app.features import broker_features

    rows = []
    for day in expected_trading_sessions(date(2026, 8, 20), 20):
        rows.extend([
            {"source_date": day, "securities_trader_id": "BUY", "net_volume": 1000},
            {"source_date": day, "securities_trader_id": "SELL", "net_volume": -1000},
        ])
    result = broker_features(rows)
    assert result["Top3BrokerConcentration20D"] == pytest.approx(1.0)
    assert 0 <= result["Top5BrokerConcentration20D"] <= 1
    assert result["PersistentBuyerCount5D"] == 1
    assert result["PersistentBuyerCount10D"] == 1
    assert result["PersistentBuyerCount20D"] == 1


def test_source_revision_and_historical_score_are_point_in_time() -> None:
    db, _ = _db()
    db.add(Stock(stock_id="2330", stock_name="Test", market="上市", security_type="股票", is_common_stock=True))
    db.commit()
    end = date(2026, 8, 20)
    sessions = expected_trading_sessions(end, 21)
    for day in sessions:
        ingest_records(db, "TaiwanStockInstitutionalInvestorsBuySellWide", [{"stock_id": "2330", "date": day, "Foreign_Investor_buy": 110, "Foreign_Investor_sell": 10, "Foreign_Dealer_Self_buy": 20, "Foreign_Dealer_Self_sell": 10, "Investment_Trust_Buy": 30, "Investment_Trust_Sell": 10, "Dealer_Buy": 15, "Dealer_Sell": 10, "Dealer_self_Buy": 5, "Dealer_self_Sell": 1, "Dealer_Hedging_Buy": 4, "Dealer_Hedging_Sell": 2}])
        ingest_records(db, "TaiwanStockShareholding", [{"stock_id": "2330", "date": day, "ForeignInvestmentShares": 1000 + day.day, "ForeignInvestmentSharesRatio": 40 + day.day / 100}])
        ingest_records(db, "TaiwanStockPrice", [{"stock_id": "2330", "date": day, "close": 100 + day.day, "TradingVolume": 100000}])
        ingest_records(db, "TaiwanStockTradingDailyReport", [{"stock_id": "2330", "date": day, "securities_trader_id": "A", "buy_volume": 100, "sell_volume": 10}])
    for day in (end - timedelta(days=28), end - timedelta(days=21), end - timedelta(days=14), end - timedelta(days=7), end):
        ingest_records(db, "TaiwanStockHoldingSharesPer", [{"stock_id": "2330", "date": day, "HoldingSharesLevel": "400,001-600,000", "percent": 10, "people": 3}])
        ingest_records(db, "TaiwanStockHoldingSharesPer", [{"stock_id": "2330", "date": day, "HoldingSharesLevel": "1,000,001-2,000,000", "percent": 5, "people": 1}])
    cutoff = datetime.now(timezone.utc)
    original = calculate_stock_features_and_score(db, "2330", end, cutoff)
    ingest_records(db, "TaiwanStockInstitutionalInvestorsBuySellWide", [{"stock_id": "2330", "date": sessions[-1], "Foreign_Investor_buy": 1, "Foreign_Investor_sell": 1000, "Foreign_Dealer_Self_buy": 20, "Foreign_Dealer_Self_sell": 10, "Investment_Trust_Buy": 30, "Investment_Trust_Sell": 10, "Dealer_Buy": 15, "Dealer_Sell": 10, "Dealer_self_Buy": 5, "Dealer_self_Sell": 1, "Dealer_Hedging_Buy": 4, "Dealer_Hedging_Sell": 2}])
    sealed = calculate_stock_features_and_score(db, "2330", end, cutoff)
    assert sealed.id == original.id
    assert sealed.score == original.score
    assert sealed.input_snapshot_hash == original.input_snapshot_hash
    assert db.scalar(select(SourceRevision).where(SourceRevision.dataset == "TaiwanStockInstitutionalInvestorsBuySellWide", SourceRevision.stock_id == "2330").order_by(SourceRevision.id.desc())) is not None
    later = calculate_stock_features_and_score(db, "2330", end, datetime.now(timezone.utc) + timedelta(seconds=1))
    assert later.id != original.id
    assert db.scalar(select(AccumulationScore.id).where(AccumulationScore.stock_id == "2330")) is not None


def test_partial_revision_batch_does_not_erase_legacy_window() -> None:
    db, _ = _db()
    db.add(Stock(stock_id="2330", stock_name="Test", market="上市", security_type="股票", is_common_stock=True))
    fetched_at = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    sessions = expected_trading_sessions(date(2026, 8, 20), 20)
    for day in sessions:
        db.add(InstitutionalDaily(stock_id="2330", source_date=day, institutional_net=1, source_dataset="TaiwanStockInstitutionalInvestorsBuySellWide", fetched_at=fetched_at))
    db.commit()
    revised = {"stock_id": "2330", "source_date": sessions[-1], "institutional_net": 2}
    db.add(SourceRevision(dataset="TaiwanStockInstitutionalInvestorsBuySellWide", stock_id="2330", source_date=sessions[-1], natural_key=_natural_key("TaiwanStockInstitutionalInvestorsBuySellWide", revised), payload={**revised, "source_date": sessions[-1].isoformat()}, content_hash="a" * 64, fetched_at=fetched_at + timedelta(hours=1)))
    db.commit()

    rows, hashes = _model_rows(db, InstitutionalDaily, "2330", sessions[-1], fetched_at + timedelta(hours=2), "TaiwanStockInstitutionalInvestorsBuySellWide", 20)

    assert [str(row["source_date"])[:10] for row in rows] == [day.isoformat() for day in sessions]
    assert rows[-1]["institutional_net"] == 2
    assert len(hashes) == 20
    assert len(set(hashes)) == 20


def test_broker_rows_are_bounded_by_sessions_not_broker_count() -> None:
    db, _ = _db()
    db.add(Stock(stock_id="2330", stock_name="Test", market="上市", security_type="股票", is_common_stock=True))
    fetched_at = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    sessions = expected_trading_sessions(date(2026, 8, 20), 20)
    for day in sessions:
        for broker_id in ("A", "B", "C"):
            db.add(BrokerDaily(stock_id="2330", source_date=day, securities_trader_id=broker_id, buy_volume=100, sell_volume=10, net_volume=90, source_dataset="TaiwanStockTradingDailyReport", fetched_at=fetched_at))
    db.commit()

    rows, _ = _model_rows(db, BrokerDaily, "2330", sessions[-1], fetched_at + timedelta(hours=1), "TaiwanStockTradingDailyReport", 2)

    assert len({str(row["source_date"])[:10] for row in rows}) == 20
    assert len(rows) == 60


def test_scheduled_catch_up_runs_all_phases_for_dynamic_multi_stock_universe() -> None:
    import asyncio

    from app.ingestion import catch_up
    from app.models import JobRun

    db, _ = _db()
    end = date(2026, 8, 20)
    sessions = expected_trading_sessions(end, 20)

    class FakeClient:
        def fetch(self, dataset: str, data_id: str | None = None, start_date: str | None = None, end_date: str | None = None, **_: object):
            if dataset == "TaiwanStockInfo":
                return ([{"stock_id": "2330", "stock_name": "Test A", "type": "twse", "security_type": "股票", "industry_category": "半導體業", "date": end}, {"stock_id": "2317", "stock_name": "Test B", "type": "twse", "security_type": "股票", "industry_category": "電子", "date": end}], {"source_date": end.isoformat()})
            stock_ids = ["2330", "2317"]
            rows: list[dict[str, object]] = []
            for stock_id in stock_ids:
                for day in sessions:
                    if dataset == "TaiwanStockInstitutionalInvestorsBuySellWide":
                        rows.append({"stock_id": stock_id, "date": day, "Foreign_Investor_buy": 10, "Foreign_Investor_sell": 1, "Foreign_Dealer_Self_buy": 2, "Foreign_Dealer_Self_sell": 1, "Investment_Trust_Buy": 3, "Investment_Trust_Sell": 1, "Dealer_Buy": 2, "Dealer_Sell": 1, "Dealer_self_Buy": 1, "Dealer_self_Sell": 0, "Dealer_Hedging_Buy": 1, "Dealer_Hedging_Sell": 0})
                    elif dataset == "TaiwanStockShareholding":
                        rows.append({"stock_id": stock_id, "date": day, "ForeignInvestmentShares": 100, "ForeignInvestmentSharesRatio": 10.0})
                    elif dataset == "TaiwanStockPrice":
                        rows.append({"stock_id": stock_id, "date": day, "close": 100, "TradingVolume": 1000})
                    elif dataset == "TaiwanStockHoldingSharesPer" and day in (end - timedelta(days=28), end - timedelta(days=21), end - timedelta(days=14), end - timedelta(days=7), end):
                        rows.append({"stock_id": stock_id, "date": day, "HoldingSharesLevel": "400,001-600,000", "percent": 10, "people": 1})
            return rows, {"source_date": end.isoformat()}

        async def fetch_broker_stocks(self, stock_ids: list[str], start_date: str, end_date: str, record_sink=None):
            rows = [{"stock_id": stock_id, "date": day, "securities_trader_id": "A", "buy_volume": 100, "sell_volume": 10} for stock_id in stock_ids for day in sessions]
            if record_sink:
                record_sink(rows)
            return {"requested": len(stock_ids), "skipped_checkpoint": 0, "success": len(stock_ids) * len(sessions), "failed": 0, "rows": len(rows), "retries": 0}

    result = asyncio.run(catch_up(db, FakeClient()))
    assert result["status"] in {"SUCCESS", "PARTIAL"}
    assert set(result["datasets"]) >= {"TaiwanStockInfo", "TaiwanStockInstitutionalInvestorsBuySellWide", "TaiwanStockShareholding", "TaiwanStockHoldingSharesPer", "TaiwanStockPrice", "TaiwanStockTradingDailyReport"}
    assert db.scalar(select(Stock.stock_id).where(Stock.is_common_stock.is_(True))) == "2330"
    jobs = db.scalars(select(JobRun)).all()
    assert {job.dataset for job in jobs} >= {"TaiwanStockInfo", "score", "TaiwanStockTradingDailyReport"}
