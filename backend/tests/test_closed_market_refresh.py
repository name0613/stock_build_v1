from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.ingestion as ingestion
import app.worker as worker
from app.calendar import market_session_state
from app.config import Settings
from app.db import SessionLocal
from app.finmind import AUTOMATIC_REFRESH_PAUSED, FinMindClient, FinMindError, FinMindRequestBudget
from app.main import app
from app.models import AccumulationScore, JobRun, StockRefreshIssue
from app.refresh_queue import queue_universe_budget_refresh


@pytest.fixture
def empty_queue():
    with SessionLocal() as db:
        db.query(JobRun).filter(JobRun.dataset.in_([ingestion.UNIVERSE_BUDGET_REFRESH_DATASET, ingestion.FAVORITE_REFRESH_DATASET])).delete(synchronize_session=False)
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(JobRun).filter(JobRun.dataset.in_([ingestion.UNIVERSE_BUDGET_REFRESH_DATASET, ingestion.FAVORITE_REFRESH_DATASET])).delete(synchronize_session=False)
        db.commit()


@pytest.mark.parametrize("stamp,allowed", [
    ("2026-09-08T08:59:59+08:00", True),
    ("2026-09-08T09:00:00+08:00", False),
    ("2026-09-08T13:29:59+08:00", False),
    ("2026-09-08T13:30:00+08:00", True),
    ("2026-09-12T10:00:00+08:00", True),
    ("2026-09-25T10:00:00+08:00", True),
    ("2027-01-04T20:00:00+08:00", False),
])
def test_hourly_queue_checks_actual_session(monkeypatch, empty_queue, stamp, allowed):
    session = market_session_state(datetime.fromisoformat(stamp))
    monkeypatch.setattr(worker, "market_session_state", lambda: session)
    monkeypatch.setattr(worker, "closed_market_target_date", lambda: date(2026, 8, 20))
    monkeypatch.setattr(worker, "_heartbeat", lambda **_: None)
    worker.run_closed_market_refresh()
    worker.run_closed_market_refresh()
    with SessionLocal() as db:
        jobs = db.query(JobRun).filter_by(dataset=ingestion.UNIVERSE_BUDGET_REFRESH_DATASET).all()
        assert len(jobs) == int(allowed)
        if allowed:
            assert jobs[0].checkpoint_state["budget"]["limit"] == 3500
            assert jobs[0].checkpoint_state["trigger"] == "closed_market_hourly"


def test_hourly_fire_is_next_whole_hour_even_weekends():
    now = datetime.fromisoformat("2026-09-12T23:59:59+08:00")
    expected = datetime.fromisoformat("2026-09-13T00:00:00+08:00")
    assert datetime.fromisoformat(worker._next_job_fire_at(worker.CLOSED_MARKET_REFRESH_JOB_ID, now)) == expected


def test_manual_auto_dedup_and_completed_hour_survives_restart(empty_queue):
    now = datetime(2026, 9, 8, 8, tzinfo=timezone.utc)
    with SessionLocal() as db:
        job, created = queue_universe_budget_refresh(db, date(2026, 8, 20), automatic=True, now=now)
        assert created
        job_id = job.id
    with TestClient(app) as client:
        response = client.post("/api/universe/refresh-and-score", params={"source_date": "2026-08-20"})
        assert response.status_code == 409
        assert response.json()["detail"]["job_id"] == job_id
    with SessionLocal() as db:
        job = db.get(JobRun, job_id)
        job.status = "SUCCESS"
        db.commit()
    with SessionLocal() as db:
        job, created = queue_universe_budget_refresh(db, date(2026, 8, 20), automatic=True, now=now + timedelta(minutes=59))
        assert not created and job.id == job_id
        job, created = queue_universe_budget_refresh(db, date(2026, 8, 20), automatic=True, now=now + timedelta(hours=1))
        assert created and job.id != job_id


def test_open_market_does_not_starve_manual_jobs(monkeypatch, empty_queue):
    with SessionLocal() as db:
        auto, _ = queue_universe_budget_refresh(db, date(2026, 8, 20), automatic=True)
        favorite = JobRun(dataset=ingestion.FAVORITE_REFRESH_DATASET, status="QUEUED", started_at=datetime.now(timezone.utc), checkpoint_state={})
        db.add(favorite)
        db.commit()
        monkeypatch.setattr(worker, "market_session_state", lambda: {"state": "OPEN"})
        assert worker._next_durable_refresh_job(db).id == favorite.id
        monkeypatch.setattr(worker, "market_session_state", lambda: {"state": "CLOSED"})
        assert worker._next_durable_refresh_job(db).id == auto.id


def test_guard_blocks_after_rate_limit_wait_without_spending_budget(monkeypatch, tmp_path):
    session = {"state": "CLOSED"}
    monkeypatch.setattr(worker, "market_session_state", lambda: session)
    monkeypatch.setattr("app.finmind.time.monotonic", lambda: 0)
    monkeypatch.setattr("app.finmind.time.sleep", lambda _: session.update(state="OPEN"))
    monkeypatch.setattr(httpx, "Client", lambda **_: pytest.fail("must not send HTTP during open market"))
    budget = FinMindRequestBudget(3500, tmp_path / "budget.json")
    client = FinMindClient(Settings(raw_root=tmp_path, finmind_api_token="test"), request_budget=budget, request_guard=worker._require_closed_market)
    client._next_request_at = 1
    with pytest.raises(FinMindError, match="confirmed closed market"):
        client.fetch("TaiwanStockPrice", "2330", "2026-08-20", "2026-08-20")
    assert budget.snapshot()["used"] == 0
    with pytest.raises(FinMindError):
        client.provider_quota(source_revision="test")


def test_auto_pause_preserves_stock_progress_budget_and_resumes(monkeypatch, tmp_path, empty_queue):
    session = {"state": "CLOSED"}
    monkeypatch.setattr(ingestion, "market_session_state", lambda: session)
    monkeypatch.setattr(ingestion, "closed_market_target_date", lambda *_: date(2026, 8, 20))
    budget = FinMindRequestBudget(2, tmp_path / "resume.json")
    client = SimpleNamespace(request_budget=budget, settings=SimpleNamespace(broker_quota_reserve=0), provider_quota=lambda **_: {"provider_reported_remaining": 6000})
    calls = []

    async def fetch(_db, provider, stock_id, _target, **kwargs):
        calls.append(kwargs["refreshed_datasets"])
        provider.request_budget.reserve()
        if len(calls) == 1:
            session["state"] = "OPEN"
            return {"datasets": {"TaiwanStockPrice": {"refresh_complete": True, "records_accepted": 1}}, "fetch_errors": [{"error_code": AUTOMATIC_REFRESH_PAUSED}]}
        return {"datasets": {name: {"refresh_complete": True, "records_accepted": 1} for name in ingestion.FAVORITE_REFRESH_DATASETS}, "fetch_errors": []}

    monkeypatch.setattr(ingestion, "fetch_and_score_stock", fetch)
    with SessionLocal() as db:
        job, _ = queue_universe_budget_refresh(db, date(2026, 8, 20), automatic=True)
        job.checkpoint_state = {**job.checkpoint_state, "stock_ids": ["2330"], "cycle_stock_ids": ["2330"]}
        db.commit()
        result = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, client, job))
        assert result["phase"] == "waiting_for_market_close"
        assert result["budget"]["used"] == 1
        assert job.checkpoint_state["queue_index"] == 0
        assert db.get(StockRefreshIssue, "2330") is None
        worker._reconcile_interrupted_jobs(db)
        result = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, client, job))
        assert len(calls) == 1 and result["budget"]["used"] == 1
        session["state"] = "CLOSED"
        result = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, client, job))
        assert result["status"] == "SUCCESS"
        assert result["budget"]["used"] == 2
        assert calls == [set(), {"TaiwanStockPrice"}]


def test_opening_before_scoring_does_not_write_score(monkeypatch):
    def guard():
        raise FinMindError(AUTOMATIC_REFRESH_PAUSED, "paused")

    client = SimpleNamespace(request_guard=guard)
    with SessionLocal() as db:
        count = db.query(AccumulationScore).count()
        result = asyncio.run(ingestion.fetch_and_score_stock(db, client, "2330", date(2026, 8, 20), force_refresh=True, refreshed_datasets=set(ingestion.FAVORITE_REFRESH_DATASETS)))
        assert result["score"] is None
        assert db.query(AccumulationScore).count() == count
