from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.calendar import CalendarUnknownError, expected_trading_sessions
from app.finmind import FinMindClient, FinMindError
from app.ingestion import _model_rows, _natural_key, calculate_stock_features_and_score, ingest_records, normalize_institutional, normalize_stock, sync_universe
from app.models import AccumulationScore, Base, BrokerDaily, DataSyncStatus, InstitutionalDaily, SourceRevision, Stock
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


def test_institutional_dealer_aggregate_is_not_double_counted() -> None:
    row = normalize_institutional({"stock_id": "2330", "date": "2026-08-20", "Foreign_Investor_Net": 10, "Foreign_Dealer_Self_Net": 2, "Investment_Trust_Net": 3, "Dealer_Net": 5, "Dealer_self_Net": 3, "Dealer_Hedging_Net": 2})
    assert row is not None
    assert row["dealer_aggregate_net"] == 5
    assert row["dealer_net"] == 5
    assert row["institutional_net"] == 20


def test_universe_reconciliation_is_raw_duplicate_rejection_and_accepted_exact() -> None:
    db, _ = _db()
    rows = [
        {"stock_id": "2330", "stock_name": "Test", "type": "twse", "security_type": "股票", "date": "2026-08-20"},
        {"stock_id": "2330", "stock_name": "Test duplicate", "type": "twse", "security_type": "股票", "date": "2026-08-20"},
        {"stock_id": "0050", "stock_name": "ETF", "type": "twse", "security_type": "ETF", "industry_category": "ETF", "date": "2026-08-20"},
        {"stock_id": "00400A", "stock_name": "invalid", "type": "twse", "security_type": "股票", "date": "2026-08-20"},
    ]

    class UniverseClient:
        def fetch(self, dataset: str):
            assert dataset == "TaiwanStockInfo"
            return rows, {"source_date": "2026-08-20"}

    assert sync_universe(db, UniverseClient()) == 1
    sync = db.get(DataSyncStatus, "TaiwanStockInfo")
    assert sync is not None
    universe = sync.metadata_json["universe"]
    assert universe["reconciliation"] == {"raw_count": 4, "duplicate_count": 1, "rejected_unique_count": 2, "accepted_common_count": 1, "reconciles": True}
    assert universe["market_counts"] == {"上市": 1}


def test_universe_accepts_provider_stock_security_type_but_excludes_instruments() -> None:
    assert normalize_stock({"stock_id": "2330", "stock_name": "Test", "type": "twse", "security_type": "stock"})["is_common_stock"] is True
    assert normalize_stock({"stock_id": "0050", "stock_name": "ETF", "type": "twse", "security_type": "ETF", "industry_category": "ETF"}) is None


def test_universe_refresh_failure_is_persisted_and_blocks_scoring() -> None:
    import asyncio

    from app.ingestion import catch_up
    from app.models import JobRun

    db, _ = _db()
    db.add(Stock(stock_id="2330", stock_name="Existing", market="上市", security_type="股票", is_common_stock=True))
    db.commit()

    class FailingUniverseClient:
        def fetch(self, dataset: str, *_args: object, **_kwargs: object):
            assert dataset == "TaiwanStockInfo"
            raise FinMindError("NETWORK_ERROR", "simulated universe refresh failure")

    result = asyncio.run(catch_up(db, FailingUniverseClient(), end_date=date(2026, 8, 20)))
    sync = db.get(DataSyncStatus, "TaiwanStockInfo")
    jobs = db.scalars(select(JobRun).where(JobRun.dataset == "TaiwanStockInfo")).all()
    assert result["fatal_code"] == "NETWORK_ERROR"
    assert result["provider_work_deferred"]["error_code"] == "NETWORK_ERROR"
    assert sync is not None and sync.status == "FAILED" and sync.last_error_code == "NETWORK_ERROR"
    assert sync.expected_latest_source_date == date(2026, 8, 20)
    assert jobs and jobs[-1].status == "FAILED" and jobs[-1].error_code == "NETWORK_ERROR"
    assert not db.scalars(select(JobRun).where(JobRun.dataset == "score")).all()


def test_universe_failure_does_not_mutate_previous_active_universe() -> None:
    db, _ = _db()
    db.add_all([
        Stock(stock_id="2330", stock_name="Existing", market="上市", security_type="股票", is_common_stock=True),
        Stock(stock_id="2317", stock_name="Inactive", market="上市", security_type="股票", is_common_stock=False),
    ])
    db.commit()

    class FailingUniverseClient:
        def fetch(self, dataset: str, *_args: object, **_kwargs: object):
            raise FinMindError("TIMEOUT", "simulated timeout")

    with pytest.raises(FinMindError) as exc:
        sync_universe(db, FailingUniverseClient(), as_of=date(2026, 8, 20))
    assert exc.value.code == "TIMEOUT"
    assert db.get(Stock, "2330").is_common_stock is True
    assert db.get(Stock, "2317").is_common_stock is False
    sync = db.get(DataSyncStatus, "TaiwanStockInfo")
    assert sync is not None and sync.status == "FAILED" and sync.expected_latest_source_date == date(2026, 8, 20)


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
        {"source_date": "2026-08-20", "HoldingSharesLevel": "400,001-600,000", "holding_shares_threshold": 400001, "percent": 10.0, "people": 3, "shares": 500000},
        {"source_date": "2026-08-20", "HoldingSharesLevel": "1,000,001-2,000,000", "holding_shares_threshold": 1000001, "percent": 4.0, "people": 1, "shares": 1500000},
        {"source_date": "2026-07-16", "HoldingSharesLevel": "400,001-600,000", "holding_shares_threshold": 400001, "percent": 8.0, "people": 2, "shares": 500000},
    ]
    result = holding_distribution_features(rows)
    assert result["LargeHolder400LotsPercent"] == 14.0
    assert result["LargeHolder1000LotsPercent"] == 4.0
    assert result["LargeHolder400Change4W"] is None
    assert result["HoldingBoundarySemantics"]["400"] == ">400 lots"


def test_holding_missing_relevant_boundary_is_unavailable() -> None:
    from app.features import holding_distribution_features
    result = holding_distribution_features([{"source_date": "2026-08-20", "holding_shares_threshold": 400001, "percent": 10, "people": 2, "shares": 500000}])
    assert result["LargeHolder400LotsPercent"] is None
    assert result["HoldingDistributionCoverage"]["available"] is False


def test_broker_unproven_omission_does_not_become_zero() -> None:
    from app.features import broker_features
    rows = [{"source_date": day, "securities_trader_id": "A", "net_volume": 10} for day in expected_trading_sessions(date(2026, 8, 20), 20)]
    assert broker_features(rows)["BrokerPersistenceScore"] is None


def test_broker_explicit_null_net_fails_closed_even_with_valid_same_session_row() -> None:
    from app.features import broker_features

    rows = []
    for day in expected_trading_sessions(date(2026, 8, 20), 20):
        rows.append({"source_date": day, "securities_trader_id": "VALID", "net_volume": 100, "provider_report_complete": True})
        rows.append({"source_date": day, "securities_trader_id": "NULL", "net_volume": None, "buy_volume": None, "sell_volume": None, "provider_report_complete": True})

    result = broker_features(rows)
    assert result["BrokerDataContract"] == {"available": False, "reason": "null_or_invalid_broker_net"}
    assert result["BrokerPersistenceScore"] is None
    assert result["BrokerOneDaySpikeRatio20D"] is None


def test_broker_omitted_branch_under_complete_report_retains_zero_absence_semantics() -> None:
    from app.features import broker_features

    rows = [{"source_date": day, "securities_trader_id": "VALID", "net_volume": 100, "provider_report_complete": True} for day in expected_trading_sessions(date(2026, 8, 20), 20)]
    result = broker_features(rows)
    assert result["BrokerDataContract"] == {"available": True, "reason": None}
    assert result["BrokerPersistenceScore"] is not None


def test_broker_concentration_is_bounded_with_offsetting_sellers() -> None:
    from app.features import broker_features

    rows = []
    for day in expected_trading_sessions(date(2026, 8, 20), 20):
        rows.extend([
            {"source_date": day, "securities_trader_id": "BUY", "net_volume": 1000, "provider_report_complete": True},
            {"source_date": day, "securities_trader_id": "SELL", "net_volume": -1000, "provider_report_complete": True},
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


def test_quota_at_first_required_source_defers_all_later_provider_requests() -> None:
    import asyncio
    from app.ingestion import catch_up

    db, _ = _db()
    end = date(2026, 8, 20)

    class QuotaClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def fetch(self, dataset: str, *_args, **_kwargs):
            self.calls.append(dataset)
            assert dataset == "TaiwanStockInfo"
            return ([{"stock_id": "2330", "stock_name": "Test", "type": "twse", "security_type": "股票", "date": end}], {"source_date": end.isoformat()})

        async def fetch_stocks_dataset(self, stock_ids, dataset, *_args, **_kwargs):
            self.calls.append(dataset)
            assert dataset == "TaiwanStockInstitutionalInvestorsBuySellWide"
            return {"requested": len(stock_ids), "success": 0, "usable_success": 0, "no_data": 0, "failed": 1, "rows": 0, "fatal_code": "QUOTA_EXHAUSTED", "per_stock": {}}

        async def fetch_broker_stocks(self, *_args, **_kwargs):
            raise AssertionError("broker requests must be deferred after global quota failure")

    client = QuotaClient()
    result = asyncio.run(catch_up(db, client, end_date=end))
    assert result["fatal_code"] == "QUOTA_EXHAUSTED"
    assert result["provider_work_deferred"]["error_code"] == "QUOTA_EXHAUSTED"
    assert client.calls == ["TaiwanStockInfo", "TaiwanStockInstitutionalInvestorsBuySellWide"]
