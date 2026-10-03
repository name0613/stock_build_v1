"""Regressions for missing holidays and runaway refresh exclusion counters."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import ingestion
from app.calendar import closed_market_target_date, completed_source_end_date, expected_trading_sessions, is_trading_session
from app.finmind import FinMindRequestBudget, expected_observation_dates
from app.models import Base, JobRun, Stock, StockRefreshIssue


@pytest.mark.parametrize("holiday", [date(2026, 2, 12), date(2026, 2, 13), date(2026, 2, 27), date(2026, 9, 28)])
def test_official_non_trading_dates_never_become_required_observations(holiday):
    assert not is_trading_session(holiday)
    for dataset in ("TaiwanStockPrice", "TaiwanStockInstitutionalInvestorsBuySellWide", "TaiwanStockShareholding"):
        assert holiday not in expected_observation_dates(dataset, holiday - timedelta(days=7), holiday + timedelta(days=7))
    assert holiday not in expected_trading_sessions(holiday + timedelta(days=2), 20)


def test_teachers_day_target_stays_on_last_real_session():
    now = datetime.fromisoformat("2026-09-28T22:00:00+08:00")
    assert closed_market_target_date(now) == completed_source_end_date(now) == date(2026, 9, 24)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(ingestion, "_now", lambda: datetime(2026, 10, 3, 4, tzinfo=timezone.utc))
    monkeypatch.setattr(ingestion, "market_session_state", lambda: {"state": "CLOSED"})
    with Session(engine, expire_on_commit=False) as session:
        session.add(Stock(stock_id="9001", stock_name="test", market="test", is_common_stock=True))
        session.commit()
        yield session
    engine.dispose()


def test_same_or_older_target_cannot_count_again_after_new_job_or_session(db):
    target = date(2026, 9, 29)
    ingestion._record_no_data_attempt(db, "9001", None, {}, target=target)
    db.commit()
    with Session(db.get_bind()) as restarted:
        for value in [target, target, date(2026, 9, 24)]:
            issue = ingestion._record_no_data_attempt(restarted, "9001", None, {}, target=value)
            restarted.commit()
            assert issue.no_data_attempts == 1
        issue = ingestion._record_no_data_attempt(restarted, "9001", None, {}, target=target + timedelta(days=1))
        restarted.commit()
        assert issue.no_data_attempts == 2


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("gap_code", ["PARTIAL_OBSERVATION_COVERAGE", "PARTIAL_RESPONSE_UNVERIFIED"])
def test_extra_history_gap_never_excludes_ready_target_and_is_not_tight_retried(db, monkeypatch, tmp_path, automatic, ready, gap_code):
    target = date(2026, 10, 2)
    ingestion._record_no_data_attempt(db, "9001", None, {}, target=date(2026, 10, 1))
    db.commit()
    job = JobRun(dataset=ingestion.UNIVERSE_BUDGET_REFRESH_DATASET, status="QUEUED", started_at=ingestion._now(), requested_end_date=target,
                 checkpoint_state={"trigger": "closed_market_hourly" if automatic else "manual", "stock_ids": ["9001", "9001"], "cycle_stock_ids": ["9001"]})
    db.add(job)
    db.commit()
    client = SimpleNamespace(request_budget=FinMindRequestBudget(5, tmp_path / "budget.json"), settings=SimpleNamespace(broker_quota_reserve=0), provider_quota=lambda **_: {"provider_reported_remaining": 6000})
    calls = []

    async def fetch(_db, client, sid, target, **kwargs):
        calls.append(sid)
        client.request_budget.reserve()
        datasets = {name: {"refresh_complete": True, "records_accepted": 20} for name in ingestion.FAVORITE_REFRESH_DATASETS}
        datasets["TaiwanStockHoldingSharesPer"] = {"refresh_complete": False, "records_accepted": 120, "failure_codes": [gap_code]}
        # Even a successful fallback must not clear target failure counters.
        return {"datasets": datasets, "fetch_errors": [], "score": {"score": 70}, "readiness": {"ready": True}, "target_readiness": {"ready": ready}, "fallback_applied": not ready}

    monkeypatch.setattr(ingestion, "fetch_and_score_stock", fetch)
    result = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, client, job))
    assert calls == ["9001"]
    assert result["phase"] == "waiting_for_source_data" and result["status"] == "PARTIAL"
    assert db.get(StockRefreshIssue, "9001").no_data_attempts == (0 if ready else 2)
    assert ingestion.skipped_refresh_stock_ids(db) == set()
    # A completed job replay must never fetch or count again.
    asyncio.run(ingestion.resume_universe_budget_refresh_job(db, client, job))
    assert calls == ["9001"]


def test_manual_ready_target_clears_exclusion_only_at_atomic_finish(db):
    issue = ingestion._record_no_data_attempt(db, "9001", None, {}, target=date(2026, 10, 1))
    issue.no_data_attempts = 5
    job = JobRun(dataset="manual_stock_refresh_score", status="RUNNING", started_at=ingestion._now(), checkpoint_state={"stock_id": "9001"})
    db.add(job)
    db.commit()
    ingestion._job_finish(db, job, "SUCCESS", checkpoint_state={"stock_id": "9001", "target_readiness": {"ready": True}})
    assert db.get(StockRefreshIssue, "9001").no_data_attempts == 0
    ingestion._record_no_data_attempt(db, "9001", None, {}, target=date(2026, 10, 2))
    db.commit()
    ingestion._job_finish(db, job, "SUCCESS", checkpoint_state={"stock_id": "9001", "target_readiness": {"ready": True}})
    assert db.get(StockRefreshIssue, "9001").no_data_attempts == 1


def test_waiting_partial_response_advances_to_next_stock_after_restart(db, monkeypatch, tmp_path):
    target = date(2026, 10, 2)
    db.add(Stock(stock_id="9002", stock_name="next", market="test", is_common_stock=True))
    sources = {name: {"refresh_complete": True, "records_accepted": 20} for name in ingestion.FAVORITE_REFRESH_DATASETS}
    missing = "TaiwanStockInstitutionalInvestorsBuySellWide"
    sources[missing] = {"refresh_complete": False, "records_accepted": 19, "failure_codes": ["PARTIAL_RESPONSE_UNVERIFIED"]}
    job = JobRun(dataset=ingestion.UNIVERSE_BUDGET_REFRESH_DATASET, status="WAITING_FOR_PROVIDER",
                 started_at=ingestion._now(), requested_end_date=target,
                 checkpoint_state={"trigger": "closed_market_hourly", "stock_ids": ["9001", "9002"],
                                   "cycle_stock_ids": ["9001", "9002"], "current_stock_id": "9001",
                                   "current_stock_progress": {"stock_id": "9001", "datasets": sources},
                                   "next_retry_at": (ingestion._now() - timedelta(seconds=1)).isoformat()})
    db.add(job)
    db.commit()
    job_id = job.id
    calls = []
    client = SimpleNamespace(request_budget=FinMindRequestBudget(3500, tmp_path / "resume-partial.json", used=1435),
                             settings=SimpleNamespace(broker_quota_reserve=0),
                             provider_quota=lambda **_: {"provider_reported_remaining": 6000})

    async def fetch(_db, client, sid, target, **kwargs):
        calls.append(sid)
        client.request_budget.reserve()
        if sid == "9002":
            return {"datasets": {}, "fetch_errors": [{"error_code": "QUOTA_EXHAUSTED"}]}
        assert kwargs["refreshed_datasets"] == set(ingestion.FAVORITE_REFRESH_DATASETS) - {missing}
        datasets = {name: {"status": "REUSED_REFRESH_CHECKPOINT", "refresh_complete": True} for name in sources}
        datasets[missing] = sources[missing]
        return {"datasets": datasets, "fetch_errors": [], "target_readiness": {"ready": False}, "score": {"score": None}}

    monkeypatch.setattr(ingestion, "fetch_and_score_stock", fetch)
    with Session(db.get_bind(), expire_on_commit=False) as restarted:
        job = restarted.get(JobRun, job_id)
        result = asyncio.run(ingestion.resume_universe_budget_refresh_job(restarted, client, job))
        assert result["status"] == "WAITING_FOR_QUOTA"
        assert result["current_stock_id"] == "9002"
        assert result["progress"]["completed"] == 1
        assert result["budget"]["used"] == 1437
        assert job.checkpoint_state["deferred_stock_ids"] == ["9001"]
        assert restarted.get(StockRefreshIssue, "9001").no_data_attempts == 1
        asyncio.run(ingestion.resume_universe_budget_refresh_job(restarted, client, job))
        assert calls == ["9001", "9002"]
