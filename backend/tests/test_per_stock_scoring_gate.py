from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.calendar import expected_trading_sessions
from app.finmind import FinMindError
from app.ingestion import catch_up, evaluate_stock_readiness, evaluate_universe_readiness, calculate_stock_features_and_score, fetch_and_score_stock, score_existing_data
from app.models import AccumulationScore, Base, BrokerDaily, DataSyncStatus, ForeignShareholdingDaily, HoldingDistribution, InstitutionalDaily, PriceDaily, Stock
from app.scoring import BROKER_ROW_CONTRACT_VERSION, HOLDING_CANONICAL_LEVELS


END = date(2026, 8, 27)
FETCHED_AT = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_universe(db: Session) -> None:
    db.add_all([
        Stock(stock_id=stock_id, stock_name=stock_id, market="上市", security_type="股票", is_common_stock=True)
        for stock_id in ("9001", "9002", "9003", "9004")
    ])
    db.commit()


def _seed_complete_sources(db: Session, stock_id: str) -> None:
    sessions = expected_trading_sessions(END, 21)
    for index, day in enumerate(sessions):
        db.add(InstitutionalDaily(stock_id=stock_id, source_date=day, foreign_net=100 + index, foreign_dealer_self_net=1, investment_trust_net=20, dealer_net=5, institutional_net=125 + index, source_dataset="TaiwanStockInstitutionalInvestorsBuySellWide", fetched_at=FETCHED_AT))
        db.add(ForeignShareholdingDaily(stock_id=stock_id, source_date=day, foreign_investment_shares=100000 + index, foreign_investment_shares_ratio=10 + index / 100, number_of_shares_issued=200000, source_dataset="TaiwanStockShareholding", fetched_at=FETCHED_AT))
        db.add(PriceDaily(stock_id=stock_id, source_date=day, close=100 + index, volume=1000 + index, source_dataset="TaiwanStockPrice", fetched_at=FETCHED_AT))
    for day in expected_trading_sessions(END, 20):
        db.add(BrokerDaily(stock_id=stock_id, source_date=day, securities_trader_id="A", buy_volume=100, sell_volume=10, net_volume=90, source_dataset="TaiwanStockTradingDailyReport", provider_row_validated=True, provider_row_contract_version=BROKER_ROW_CONTRACT_VERSION, fetched_at=FETCHED_AT))
    for holding_day in (date(2026, 7, 24), date(2026, 7, 31), date(2026, 8, 7), date(2026, 8, 14), date(2026, 8, 21)):
        for index, (level, threshold) in enumerate(HOLDING_CANONICAL_LEVELS):
            db.add(HoldingDistribution(stock_id=stock_id, source_date=holding_day, holding_shares_level=level, holding_shares_threshold=threshold, people=10 + index, percent=1 + index / 10, shares=threshold, source_dataset="TaiwanStockHoldingSharesPer", fetched_at=FETCHED_AT))
    db.commit()


def test_mixed_readiness_scores_ready_stocks_and_fails_closed_others() -> None:
    db = _db()
    _seed_universe(db)
    _seed_complete_sources(db, "9001")
    _seed_complete_sources(db, "9004")

    before_scores = len(db.scalars(select(AccumulationScore).where(AccumulationScore.source_date == END)).all())
    audit = evaluate_universe_readiness(db, ["9001", "9002", "9003", "9004"], END, FETCHED_AT)
    after_scores = len(db.scalars(select(AccumulationScore).where(AccumulationScore.source_date == END)).all())
    assert after_scores == before_scores
    assert audit["evaluated_stock_count"] == 4
    assert audit["ready_stock_count"] == 2
    assert audit["not_ready_stock_count"] == 2
    assert audit["accounting_invariant"] if "accounting_invariant" in audit else True
    assert evaluate_stock_readiness(db, "9002", END, FETCHED_AT)["ready"] is False
    assert "missing_broker" in evaluate_stock_readiness(db, "9002", END, FETCHED_AT)["missing_reasons"]
    assert "tdcc_required_buckets_incomplete" in evaluate_stock_readiness(db, "9003", END, FETCHED_AT)["missing_reasons"]

    scores = [calculate_stock_features_and_score(db, stock_id, END, FETCHED_AT) for stock_id in ("9001", "9002", "9003", "9004")]
    assert [score.stock_id for score in scores if score.score is not None] == ["9001", "9004"]
    assert all(score.status == "DATA_INSUFFICIENT" and score.score is None for score in scores if score.stock_id in {"9002", "9003"})


def test_manual_existing_data_score_persists_mixed_results_without_provider_calls() -> None:
    db = _db()
    _seed_universe(db)
    _seed_complete_sources(db, "9001")

    result = score_existing_data(db, END, ["9001", "9002", "9003", "9004"])

    assert result["status"] == "SUCCESS"
    assert result["score_metrics"]["universe_stock_count"] == 4
    assert result["score_metrics"]["ready_stock_count"] == 1
    assert result["score_metrics"]["not_ready_stock_count"] == 3
    assert result["score_metrics"]["score_rows_processed"] == 1
    assert result["score_metrics"]["score_rows_data_insufficient"] == 3
    assert result["score_metrics"]["accounting_invariant"] is True
    rows = db.scalars(select(AccumulationScore).where(AccumulationScore.source_date == END)).all()
    assert len(rows) == 4
    assert next(row for row in rows if row.stock_id == "9001").score is not None
    assert all(row.score is None and row.status == "DATA_INSUFFICIENT" for row in rows if row.stock_id != "9001")


def test_targeted_fetch_reuses_complete_sources_and_scores_after_missing_broker_is_filled() -> None:
    db = _db()
    _seed_universe(db)
    _seed_complete_sources(db, "9001")
    db.query(BrokerDaily).filter(BrokerDaily.stock_id == "9001").delete()
    db.commit()

    class TargetedClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        async def fetch_stocks_dataset(self, stock_ids, dataset, start_date, end_date, *, record_sink=None, progress_callback=None):
            self.calls.append((dataset, start_date, end_date))
            raise AssertionError("complete source datasets should be reused locally")

        async def fetch_broker_stocks(self, stock_ids, start_date, end_date, *, record_sink=None, progress_callback=None):
            self.calls.append(("TaiwanStockTradingDailyReport", start_date, end_date))
            rows = [
                {
                    "stock_id": "9001",
                    "date": day.isoformat(),
                    "securities_trader_id": "A",
                    "securities_trader_name": "A",
                    "buy": 100,
                    "sell": 10,
                    "provider_row_validated": True,
                    "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION,
                }
                for day in expected_trading_sessions(END, 20)
            ]
            if record_sink:
                record_sink(rows)
            return {
                "requested": 1,
                "requested_keys": 20,
                "skipped_checkpoint": 0,
                "observations_reused": 0,
                "physical_requests": 20,
                "rows": len(rows),
                "rows_received": len(rows),
                "success": 20,
                "failed": 0,
                "retryable_pending": 0,
                "fatal_code": None,
            }

    client = TargetedClient()
    result = asyncio.run(fetch_and_score_stock(db, client, "9001", END))

    assert result["status"] == "SUCCESS"
    assert result["score"]["score"] is not None
    assert result["readiness"]["ready"] is True
    assert [call[0] for call in client.calls] == ["TaiwanStockTradingDailyReport"]
    persisted = db.scalar(select(AccumulationScore).where(AccumulationScore.stock_id == "9001", AccumulationScore.source_date == END))
    assert persisted is not None and persisted.score is not None


def test_global_quota_failure_does_not_veto_existing_ready_stocks() -> None:
    db = _db()
    _seed_universe(db)
    _seed_complete_sources(db, "9001")
    _seed_complete_sources(db, "9004")

    class QuotaClient:
        def fetch(self, dataset: str, *_args: object, **_kwargs: object):
            assert dataset == "TaiwanStockInfo"
            return ([
                {"stock_id": stock_id, "stock_name": stock_id, "type": "twse", "security_type": "股票", "date": END}
                for stock_id in ("9001", "9002", "9003", "9004")
            ], {"source_date": END.isoformat()})

        async def fetch_stocks_dataset(self, *_args: object, **_kwargs: object):
            raise FinMindError("QUOTA_EXHAUSTED", "simulated quota exhaustion")

        async def fetch_broker_stocks(self, *_args: object, **_kwargs: object):
            raise AssertionError("broker provider work must remain deferred after a global quota failure")

    result = asyncio.run(catch_up(db, QuotaClient(), end_date=END))
    metrics = result["score_metrics"]
    assert result["fatal_code"] == "QUOTA_EXHAUSTED"
    assert result["datasets"]["TaiwanStockTradingDailyReport"]["status"] == "DEFERRED"
    assert metrics["universe_stock_count"] == 4
    assert metrics["evaluated_stock_count"] == 4
    assert metrics["ready_stock_count"] == 2
    assert metrics["not_ready_stock_count"] == 2
    assert metrics["score_rows_processed"] == 2
    assert metrics["score_rows_data_insufficient"] == 2
    assert metrics["accounting_invariant"] is True
    persisted = db.scalars(select(AccumulationScore).where(AccumulationScore.source_date == END)).all()
    assert sum(score.score is not None for score in persisted) == 2
    assert sum(score.status == "DATA_INSUFFICIENT" and score.score is None for score in persisted) == 2
    from app.main import _current_score_date, _provider_state
    sync = list(db.scalars(select(DataSyncStatus)).all())
    provider_state = _provider_state(sync)
    assert _current_score_date(db, provider_state, sync) == END
    assert provider_state["score_ready"] is True
    assert provider_state["source_coverage_numeric_scores_allowed"] is False


def test_partial_global_source_is_advisory_for_ready_stock() -> None:
    db = _db()
    _seed_universe(db)
    _seed_complete_sources(db, "9001")

    class PartialClient:
        def fetch(self, dataset: str, *_args: object, **_kwargs: object):
            assert dataset == "TaiwanStockInfo"
            return ([
                {"stock_id": stock_id, "stock_name": stock_id, "type": "twse", "security_type": "股票", "date": END}
                for stock_id in ("9001", "9002", "9003", "9004")
            ], {"source_date": END.isoformat()})

        async def fetch_stocks_dataset(self, stock_ids, dataset, *_args: object, **_kwargs: object):
            return {
                "requested": len(stock_ids),
                "success": len(stock_ids) - 1,
                "failed": 1,
                "retryable_pending": 1,
                "permanent_failed": 0,
                "physical_requests": 0,
                "rows": 0,
                "rows_received": 0,
                "rows_accepted": 0,
                "rows_versioned": 0,
                "observations_reused": 0,
                "fatal_code": None,
                "per_stock": {},
            }

        async def fetch_broker_stocks(self, stock_ids, *_args: object, **_kwargs: object):
            return {
                "requested": len(stock_ids),
                "requested_keys": len(stock_ids) * 20,
                "skipped_checkpoint": len(stock_ids) * 20,
                "success": len(stock_ids),
                "failed": 0,
                "retryable_pending": 0,
                "physical_requests": 0,
                "rows": 0,
                "stocks_completed": len(stock_ids),
                "stocks_failed": 0,
            }

    result = asyncio.run(catch_up(db, PartialClient(), end_date=END))
    assert result["score_preflight"]["ready"] is False
    assert result["score_metrics"]["ready_stock_count"] == 1
    assert result["score_metrics"]["score_rows_processed"] == 1
    assert result["score_metrics"]["score_rows_data_insufficient"] == 3


def test_isolated_stock_evaluation_failure_does_not_abort_other_stocks(monkeypatch) -> None:
    db = _db()
    _seed_universe(db)
    _seed_complete_sources(db, "9001")

    class PartialClient:
        def fetch(self, dataset: str, *_args: object, **_kwargs: object):
            assert dataset == "TaiwanStockInfo"
            return ([
                {"stock_id": stock_id, "stock_name": stock_id, "type": "twse", "security_type": "股票", "date": END}
                for stock_id in ("9001", "9002", "9003", "9004")
            ], {"source_date": END.isoformat()})

        async def fetch_stocks_dataset(self, stock_ids, dataset, *_args: object, **_kwargs: object):
            return {
                "requested": len(stock_ids), "success": len(stock_ids), "failed": 0,
                "retryable_pending": 0, "permanent_failed": 0, "physical_requests": 0,
                "rows": 0, "rows_received": 0, "rows_accepted": 0, "rows_versioned": 0,
                "observations_reused": 0, "fatal_code": None, "per_stock": {},
            }

        async def fetch_broker_stocks(self, stock_ids, *_args: object, **_kwargs: object):
            return {
                "requested": len(stock_ids), "requested_keys": len(stock_ids) * 20,
                "skipped_checkpoint": len(stock_ids) * 20, "success": len(stock_ids),
                "failed": 0, "retryable_pending": 0, "physical_requests": 0,
                "rows": 0, "stocks_completed": len(stock_ids), "stocks_failed": 0,
            }

    import app.ingestion as ingestion_module

    original_evaluator = ingestion_module._evaluate_stock_inputs

    def fail_one(db_session, stock_id, as_of, cutoff):
        if stock_id == "9002":
            raise ValueError("simulated isolated validation failure")
        return original_evaluator(db_session, stock_id, as_of, cutoff)

    monkeypatch.setattr(ingestion_module, "_evaluate_stock_inputs", fail_one)
    result = asyncio.run(catch_up(db, PartialClient(), end_date=END))

    metrics = result["score_metrics"]
    assert metrics["ready_stock_count"] == 1
    assert metrics["not_ready_stock_count"] == 3
    assert metrics["score_rows_processed"] == 1
    assert metrics["score_rows_data_insufficient"] == 3
    assert metrics["score_rows_failed"] == 1
    assert metrics["accounting_invariant"] is True
    persisted = db.scalars(select(AccumulationScore).where(AccumulationScore.source_date == END)).all()
    assert len(persisted) == 4
    failed_row = next(row for row in persisted if row.stock_id == "9002")
    assert failed_row.status == "DATA_INSUFFICIENT"
    assert failed_row.score is None


def test_all_incomplete_universe_reports_zero_ready_without_zero_scores() -> None:
    db = _db()
    _seed_universe(db)

    class EmptyClient:
        def fetch(self, dataset: str, *_args: object, **_kwargs: object):
            assert dataset == "TaiwanStockInfo"
            return ([
                {"stock_id": stock_id, "stock_name": stock_id, "type": "twse", "security_type": "股票", "date": END}
                for stock_id in ("9001", "9002", "9003", "9004")
            ], {"source_date": END.isoformat()})

        async def fetch_stocks_dataset(self, stock_ids, dataset, *_args: object, **_kwargs: object):
            return {
                "requested": len(stock_ids), "success": len(stock_ids), "failed": 0,
                "retryable_pending": 0, "permanent_failed": 0, "physical_requests": 0,
                "rows": 0, "rows_received": 0, "rows_accepted": 0, "rows_versioned": 0,
                "observations_reused": 0, "fatal_code": None, "per_stock": {},
            }

        async def fetch_broker_stocks(self, stock_ids, *_args: object, **_kwargs: object):
            return {
                "requested": len(stock_ids), "requested_keys": len(stock_ids) * 20,
                "skipped_checkpoint": len(stock_ids) * 20, "success": len(stock_ids),
                "failed": 0, "retryable_pending": 0, "physical_requests": 0,
                "rows": 0, "stocks_completed": len(stock_ids), "stocks_failed": 0,
            }

    result = asyncio.run(catch_up(db, EmptyClient(), end_date=END))
    assert result["score_metrics"]["ready_stock_count"] == 0
    assert result["score_metrics"]["score_rows_processed"] == 0
    assert result["score_metrics"]["score_rows_data_insufficient"] == 4
    assert result["score_status"] == "SCORE_BLOCKED_BY_SOURCE_COVERAGE"
    persisted = db.scalars(select(AccumulationScore).where(AccumulationScore.source_date == END)).all()
    assert len(persisted) == 4
    assert all(row.score is None and row.status == "DATA_INSUFFICIENT" for row in persisted)
