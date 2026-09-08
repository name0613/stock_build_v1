from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fastapi.testclient import TestClient

import app.finmind as finmind_module
import app.ingestion as ingestion_module
from app.db import SessionLocal
from app.ingestion import FAVORITE_REFRESH_DATASET, FAVORITE_REFRESH_DATASETS, resume_favorite_refresh_job
from app.main import app as api_app
from app.models import JobRun, Stock
from app.worker import _reconcile_interrupted_jobs


def test_quota_endpoint_returns_only_sanitized_counters(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, _settings) -> None:
            pass

        def provider_quota(self, *, source_revision: str):
            assert source_revision
            return {
                "status": "PASS",
                "provider_reported_remaining": 4321,
                "provider_reported_limit_per_hour": 6000,
                "provider_reported_used": 1679,
                "plan": "Sponsor",
                "generated_at": "2026-08-30T10:00:00+00:00",
                "authorization": "must-not-leak",
            }

    monkeypatch.setattr(finmind_module, "FinMindClient", FakeClient)
    with TestClient(api_app) as client:
        response = client.get("/api/finmind/quota")
    assert response.status_code == 200
    assert response.json() == {
        "status": "PASS",
        "remaining": 4321,
        "limit_per_hour": 6000,
        "used": 1679,
        "plan": "Sponsor",
        "checked_at": "2026-08-30T10:00:00+00:00",
    }
    assert "authorization" not in response.text.lower()
    assert "token" not in response.text.lower()


def test_favorite_refresh_queue_captures_current_descending_score_order() -> None:
    with SessionLocal() as db:
        db.query(JobRun).filter(JobRun.dataset == FAVORITE_REFRESH_DATASET).delete(synchronize_session=False)
        for stock_id in ("2330", "2317", "1103"):
            db.get(Stock, stock_id).is_favorite = True
        db.commit()
    try:
        with TestClient(api_app) as client:
            response = client.post("/api/favorites/fetch-and-score", params={"source_date": "2026-08-20"})
            assert response.status_code == 202
            payload = response.json()
            status = client.get("/api/favorites/fetch-and-score", params={"job_id": payload["job_id"]})
        assert payload["status"] == "QUEUED"
        assert payload["ordered_stock_ids"] == ["2330", "2317", "1103"]
        assert payload["progress"] == {"completed": 0, "total": 3}
        assert status.json()["ordered_stock_ids"] == payload["ordered_stock_ids"]
    finally:
        with SessionLocal() as db:
            db.query(JobRun).filter(JobRun.dataset == FAVORITE_REFRESH_DATASET).delete(synchronize_session=False)
            for stock_id in ("2330", "2317", "1103"):
                db.get(Stock, stock_id).is_favorite = False
            db.commit()


def test_favorite_refresh_resumes_same_stock_after_quota_and_skips_finished_work(monkeypatch) -> None:
    with SessionLocal() as db:
        job = JobRun(
            dataset=FAVORITE_REFRESH_DATASET,
            requested_date=date(2026, 8, 20),
            requested_start_date=date(2026, 8, 20),
            requested_end_date=date(2026, 8, 20),
            status="QUEUED",
            started_at=datetime.now(timezone.utc),
            stocks_attempted=2,
            checkpoint_state={
                "phase": "queued",
                "stock_ids": ["2330", "2317"],
                "completed_stock_ids": [],
                "stock_progress": {},
            },
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    calls: list[tuple[str, set[str]]] = []

    class FakeClient:
        settings = SimpleNamespace(source_revision="test", broker_quota_reserve=100)

        def provider_quota(self, *, source_revision: str):
            return {"provider_reported_remaining": 1000, "provider_reported_limit_per_hour": 6000, "plan": "Sponsor"}

    async def fake_fetch(_db, _client, stock_id, _target, *, progress_callback, force_refresh, refreshed_datasets):
        assert force_refresh is True
        refreshed = set(refreshed_datasets)
        calls.append((stock_id, refreshed))
        if stock_id == "2317" and not refreshed:
            datasets = {
                FAVORITE_REFRESH_DATASETS[0]: {"refresh_complete": True},
                FAVORITE_REFRESH_DATASETS[-1]: {"refresh_complete": False, "quota_unselected_pending_count": 10},
            }
            return {"datasets": datasets, "score": {"score": 75.0}, "readiness": {"ready": True}, "fetch_errors": [{"dataset": FAVORITE_REFRESH_DATASETS[-1], "error_code": "QUOTA_EXHAUSTED"}]}
        datasets = {dataset: {"refresh_complete": True} for dataset in FAVORITE_REFRESH_DATASETS}
        return {"datasets": datasets, "score": {"score": 80.0}, "readiness": {"ready": True}, "fetch_errors": []}

    monkeypatch.setattr(ingestion_module, "fetch_and_score_stock", fake_fetch)
    with SessionLocal() as db:
        job = db.get(JobRun, job_id)
        first = asyncio.run(resume_favorite_refresh_job(db, FakeClient(), job))
        assert first["status"] == "WAITING_FOR_QUOTA"
        assert first["completed_stock_ids"] == ["2330"]
        checkpoint = dict(job.checkpoint_state)
        checkpoint["next_retry_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        job.checkpoint_state = checkpoint
        db.commit()
        second = asyncio.run(resume_favorite_refresh_job(db, FakeClient(), job))
        assert second["status"] == "SUCCESS"
        assert second["completed_stock_ids"] == ["2330", "2317"]
        assert second["progress"] == {"completed": 2, "total": 2}
        db.delete(job)
        db.commit()

    assert calls == [
        ("2330", set()),
        ("2317", set()),
        ("2317", {FAVORITE_REFRESH_DATASETS[0]}),
    ]


def test_worker_restart_requeues_favorite_refresh_with_checkpoint_intact() -> None:
    with SessionLocal() as db:
        job = JobRun(
            dataset=FAVORITE_REFRESH_DATASET,
            requested_date=date(2026, 8, 20),
            requested_start_date=date(2026, 8, 20),
            requested_end_date=date(2026, 8, 20),
            status="RUNNING",
            started_at=datetime.now(timezone.utc),
            stocks_attempted=2,
            stocks_completed=1,
            checkpoint_state={"phase": "refreshing_stock", "stock_ids": ["2330", "2317"], "completed_stock_ids": ["2330"], "current_stock_id": "2317"},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        _reconcile_interrupted_jobs(db)
        db.refresh(job)
        assert job.status == "QUEUED"
        assert job.error_code == "WORKER_RESTARTED_RESUMING"
        assert job.checkpoint_state["completed_stock_ids"] == ["2330"]
        assert job.checkpoint_state["phase"] == "queued_after_worker_restart"
        db.delete(job)
        db.commit()


@pytest.fixture
def favorite_gap_job():
    with SessionLocal() as db:
        job = JobRun(dataset=FAVORITE_REFRESH_DATASET, status="QUEUED", started_at=datetime.now(timezone.utc), requested_end_date=date(2026, 9, 7), stocks_attempted=2, checkpoint_state={"stock_ids": ["2330", "2317"], "completed_stock_ids": [], "stock_progress": {}})
        db.add(job)
        db.commit()
        job_id = job.id
    yield job_id
    with SessionLocal() as db:
        db.query(JobRun).filter_by(id=job_id).delete()
        db.commit()


class _FavoriteClient:
    settings = SimpleNamespace(source_revision="test", broker_quota_reserve=100)

    def provider_quota(self, **_kwargs):
        return {"provider_reported_remaining": 5926, "provider_reported_limit_per_hour": 6000}


@pytest.mark.parametrize("gap_code,ready", [("PARTIAL_OBSERVATION_COVERAGE", True), ("PARTIAL_OBSERVATION_COVERAGE", False), ("EMPTY_RESPONSE_UNVERIFIED", False)])
def test_proven_stock_gaps_keep_score_and_advance_after_restart(monkeypatch, favorite_gap_job, gap_code, ready):
    job_id = favorite_gap_job
    holding = "TaiwanStockHoldingSharesPer"
    calls = []
    with SessionLocal() as db:
        job = db.get(JobRun, job_id)
        # Reproduce production: the oldest requested holding week is missing,
        # four datasets are complete and the job has already retried 95 times.
        previous = {dataset: {"refresh_complete": True, "records_accepted": 21} for dataset in FAVORITE_REFRESH_DATASETS if dataset != holding}
        previous[holding] = {"refresh_complete": False, "failure_codes": [gap_code], "retryable_pending": 1, "per_stock": {"2330": {"retry_count": 95, "unresolved_dates": ["2026-07-10"]}}}
        job.status = "WAITING_FOR_PROVIDER"
        job.checkpoint_state = {**job.checkpoint_state, "current_stock_id": "2330", "stock_progress": {"2330": {"datasets": previous}}, "next_retry_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        db.commit()

    async def fake_fetch(_db, _client, stock_id, _target, **kwargs):
        calls.append(stock_id)
        if stock_id == "2330":
            assert kwargs["refreshed_datasets"] == set(FAVORITE_REFRESH_DATASETS) - {holding}
            datasets = {dataset: {"status": "REUSED_REFRESH_CHECKPOINT", "refresh_complete": True} for dataset in FAVORITE_REFRESH_DATASETS}
            datasets[holding] = {"refresh_complete": False, "failure_codes": [gap_code], "records_accepted": 120 if gap_code.startswith("PARTIAL") else 0, "retryable_pending": 1, "per_stock": {stock_id: {"unresolved_dates": ["2026-07-10"]}}}
            return {"datasets": datasets, "score": {"score": 68.52 if ready else None, "status": "ACCUMULATION" if ready else "DATA_INSUFFICIENT"}, "readiness": {"ready": ready}, "fetch_errors": []}
        if calls.count("2317") == 1:
            return {"datasets": {}, "fetch_errors": [{"error_code": "QUOTA_EXHAUSTED"}]}
        return {"datasets": {dataset: {"refresh_complete": True, "records_accepted": 21} for dataset in FAVORITE_REFRESH_DATASETS}, "fetch_errors": []}

    monkeypatch.setattr(ingestion_module, "fetch_and_score_stock", fake_fetch)
    with SessionLocal() as db:
        job = db.get(JobRun, job_id)
        first = asyncio.run(resume_favorite_refresh_job(db, _FavoriteClient(), job))
        assert first["status"] == "WAITING_FOR_QUOTA"
        assert first["current_stock_id"] == "2317"
        assert first["partial_stock_ids"] == ["2330"]
        assert first["progress"] == {"completed": 1, "total": 2}
        result = first["stock_progress"]["2330"]
        assert result["score"]["score"] == (68.52 if ready else None)
        assert result["score"]["status"] == ("ACCUMULATION" if ready else "DATA_INSUFFICIENT")
        assert result["datasets"][holding]["refresh_complete"] is False
        assert result["datasets"]["TaiwanStockPrice"]["records_accepted"] == 21
        job.checkpoint_state = {**job.checkpoint_state, "next_retry_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        db.commit()
    # A fresh session models a worker restart; the partial stock is complete
    # for this batch and must never return to the front of the queue.
    with SessionLocal() as db:
        job = db.get(JobRun, job_id)
        final = asyncio.run(resume_favorite_refresh_job(db, _FavoriteClient(), job))
        assert final["status"] == "PARTIAL"
        assert final["partial_stock_count"] == 1
        assert final["progress"] == {"completed": 2, "total": 2}
        assert job.stocks_failed == 1
        assert final["next_retry_at"] is None
        assert final["current_stock_id"] is None
        asyncio.run(resume_favorite_refresh_job(db, _FavoriteClient(), job))
        assert calls == ["2330", "2317", "2317"]
    with TestClient(api_app) as api:
        payload = api.get("/api/favorites/fetch-and-score", params={"job_id": job_id}).json()
        assert payload["status"] == "PARTIAL"
        assert payload["partial_stock_ids"] == ["2330"]


@pytest.mark.parametrize("failure,expected", [("NETWORK_ERROR", "WAITING_FOR_PROVIDER"), ("UNKNOWN", "WAITING_FOR_PROVIDER"), ("UNATTEMPTED", "WAITING_FOR_PROVIDER"), ("QUOTA_EXHAUSTED", "WAITING_FOR_QUOTA"), ("AUTHENTICATION_FAILED", "FAILED")])
def test_provider_failures_are_not_mislabeled_as_completed_gaps(monkeypatch, favorite_gap_job, failure, expected):
    calls = []

    async def fake_fetch(_db, _client, stock_id, _target, **kwargs):
        calls.append(stock_id)
        datasets = {dataset: {"refresh_complete": True} for dataset in FAVORITE_REFRESH_DATASETS}
        datasets["TaiwanStockHoldingSharesPer"] = {"refresh_complete": False, "failure_codes": ["PARTIAL_OBSERVATION_COVERAGE"]}
        errors = []
        if failure == "UNATTEMPTED":
            datasets.pop("TaiwanStockPrice")
        elif failure in {"NETWORK_ERROR", "UNKNOWN"}:
            datasets["TaiwanStockPrice"] = {"refresh_complete": False, "failure_codes": [failure]}
        else:
            errors = [{"error_code": failure}]
        return {"datasets": datasets, "readiness": {"ready": True}, "score": {"score": 68.52}, "fetch_errors": errors}

    monkeypatch.setattr(ingestion_module, "fetch_and_score_stock", fake_fetch)
    with SessionLocal() as db:
        result = asyncio.run(resume_favorite_refresh_job(db, _FavoriteClient(), db.get(JobRun, favorite_gap_job)))
        assert result["status"] == expected
        assert result["partial_stock_count"] == 0
        assert result["progress"]["completed"] == 0
        assert calls == ["2330"]
