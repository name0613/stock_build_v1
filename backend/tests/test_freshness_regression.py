from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import ingestion
from app.calendar import CALENDAR_HASH
from app.main import CURRENT_SCORE_DATASETS, _current_score_readiness, _provider_state, _score_history
from app.models import AccumulationScore, Base, DataSyncStatus, JobRun, Stock
from app.scoring import FORMULA_HASH, SCORE_VERSION


CURRENT_SOURCE_DATE = date(2026, 8, 26)
HISTORICAL_TARGET = date(2026, 8, 24)
CURRENT_TIME = datetime(2026, 8, 26, 23, 30, tzinfo=timezone.utc)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_historical_catch_up_keeps_current_expected_date_and_attempt_provenance(monkeypatch) -> None:
    monkeypatch.setattr(ingestion, "completed_source_end_date", lambda _now=None: CURRENT_SOURCE_DATE)
    db = _db()
    db.add(Stock(stock_id="2330", stock_name="Test", market="上市", security_type="股票", is_common_stock=True))
    db.commit()

    class HistoricalClient:
        def fetch(self, dataset: str, *_args: object, **_kwargs: object):
            assert dataset == "TaiwanStockInfo"
            return ([{"stock_id": "2330", "stock_name": "Test", "type": "twse", "security_type": "股票", "date": CURRENT_SOURCE_DATE}], {"source_date": CURRENT_SOURCE_DATE.isoformat()})

        async def fetch_stocks_dataset(self, stock_ids, dataset, *_args, **_kwargs):
            assert dataset == "TaiwanStockInstitutionalInvestorsBuySellWide"
            return {
                "requested": len(stock_ids), "success": 0, "failed": len(stock_ids),
                "rows": 0, "rows_received": 0, "rows_accepted": 0, "rows_versioned": 0,
                "retryable_pending": 0, "permanent_failed": 0, "physical_requests": 0,
                "fatal_code": "QUOTA_EXHAUSTED", "per_stock": {},
            }

    result = asyncio.run(ingestion.catch_up(db, HistoricalClient(), end_date=HISTORICAL_TARGET))
    assert result["fatal_code"] == "QUOTA_EXHAUSTED"
    info = db.get(DataSyncStatus, "TaiwanStockInfo")
    institutional = db.get(DataSyncStatus, "TaiwanStockInstitutionalInvestorsBuySellWide")
    assert info is not None and info.expected_latest_source_date == CURRENT_SOURCE_DATE
    assert institutional is not None and institutional.expected_latest_source_date == CURRENT_SOURCE_DATE
    attempts = db.scalars(select(JobRun).where(JobRun.dataset.in_((
        "TaiwanStockInfo", "TaiwanStockInstitutionalInvestorsBuySellWide"
    )))).all()
    assert attempts
    assert all(job.requested_end_date == HISTORICAL_TARGET for job in attempts)


def test_old_successful_score_cannot_become_current_after_historical_backfill(monkeypatch) -> None:
    monkeypatch.setattr(ingestion, "completed_source_end_date", lambda _now=None: CURRENT_SOURCE_DATE)
    db = _db()
    db.add(Stock(stock_id="2330", stock_name="Test", market="上市", security_type="股票", is_common_stock=True))
    for dataset in CURRENT_SCORE_DATASETS:
        latest = date(2026, 8, 21) if dataset == "TaiwanStockHoldingSharesPer" else CURRENT_SOURCE_DATE
        metadata = {"coverage": {"holding_schema": {"complete": True, "required_bucket_count": 15}}} if dataset == "TaiwanStockHoldingSharesPer" else {}
        db.add(DataSyncStatus(
            dataset=dataset, status="SUCCESS", latest_source_date=latest,
            # Simulate the old persisted value left by an explicit historical run.
            expected_latest_source_date=HISTORICAL_TARGET,
            staleness_state="FRESH", records=1, metadata_json=metadata,
        ))
    db.add(AccumulationScore(
        stock_id="2330", source_date=HISTORICAL_TARGET, score=88.0, status="STRONG_ACCUMULATION",
        score_version=SCORE_VERSION, components={"S": 88.0}, explanation=[], coverage={},
        calculated_at=CURRENT_TIME, knowledge_cutoff=CURRENT_TIME, input_source_hashes=[],
        formula_hash=FORMULA_HASH,
    ))
    db.add(JobRun(
        dataset="score", requested_date=HISTORICAL_TARGET, requested_start_date=HISTORICAL_TARGET,
        requested_end_date=HISTORICAL_TARGET, status="SUCCESS", started_at=CURRENT_TIME,
        finished_at=CURRENT_TIME, stocks_attempted=1, stocks_completed=1,
        checkpoint_state={"target_date": HISTORICAL_TARGET.isoformat(), "score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH, "calendar_hash": CALENDAR_HASH},
    ))
    db.commit()

    sync = list(db.scalars(select(DataSyncStatus)).all())
    provider_state = _provider_state(sync)
    readiness = _current_score_readiness(db, provider_state, sync)
    assert provider_state["numeric_scores_allowed"] is True
    assert readiness["target_date"] == CURRENT_SOURCE_DATE.isoformat()
    assert readiness["ready"] is False
    assert readiness["reason_code"] == "CURRENT_SCORE_JOB_NOT_SUCCESSFUL"
    assert _score_history(db, "2330", 10)[0]["source_date"] == HISTORICAL_TARGET


def test_current_policy_advances_and_holding_target_remains_weekly(monkeypatch) -> None:
    current = {"value": CURRENT_SOURCE_DATE}
    monkeypatch.setattr(ingestion, "completed_source_end_date", lambda _now=None: current["value"])
    db = _db()
    ingestion._mark_sync(db, "TaiwanStockPrice", "SUCCESS", 1, CURRENT_SOURCE_DATE, fetched_at=CURRENT_TIME, expected_latest=HISTORICAL_TARGET)
    assert db.get(DataSyncStatus, "TaiwanStockPrice").expected_latest_source_date == CURRENT_SOURCE_DATE

    current["value"] = date(2026, 8, 27)
    ingestion._mark_sync(db, "TaiwanStockPrice", "SUCCESS", 1, date(2026, 8, 27), fetched_at=CURRENT_TIME, expected_latest=date(2026, 8, 24))
    assert db.get(DataSyncStatus, "TaiwanStockPrice").expected_latest_source_date == date(2026, 8, 27)
    assert ingestion.authoritative_expected_latest_source_date("TaiwanStockHoldingSharesPer") == date(2026, 8, 21)
    current["value"] = date(2026, 8, 31)
    assert ingestion.authoritative_expected_latest_source_date("TaiwanStockHoldingSharesPer") == date(2026, 8, 28)


def test_current_expected_date_survives_database_session_restart(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(ingestion, "completed_source_end_date", lambda _now=None: CURRENT_SOURCE_DATE)
    engine = create_engine(f"sqlite:///{(tmp_path / 'freshness.db').as_posix()}")
    Base.metadata.create_all(engine)
    first_session = Session(engine)
    ingestion._mark_sync(first_session, "TaiwanStockPrice", "SUCCESS", 1, CURRENT_SOURCE_DATE, fetched_at=CURRENT_TIME, expected_latest=HISTORICAL_TARGET)
    first_session.commit()
    first_session.close()

    restarted_session = Session(engine)
    persisted = restarted_session.get(DataSyncStatus, "TaiwanStockPrice")
    assert persisted is not None and persisted.expected_latest_source_date == CURRENT_SOURCE_DATE
    restarted_session.close()
