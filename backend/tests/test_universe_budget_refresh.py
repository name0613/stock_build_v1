from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.ingestion as ingestion_module
from app.config import Settings
from app.db import SessionLocal
from app.finmind import FinMindClient, FinMindError, FinMindRequestBudget
from app.ingestion import FAVORITE_REFRESH_DATASETS, UNIVERSE_BUDGET_REFRESH_DATASET, resume_universe_budget_refresh_job
from app.main import app as api_app
from app.models import JobRun, Stock, StockRefreshIssue
from app.worker import _next_durable_refresh_job


class _Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, *_args, **_kwargs):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_request_budget_counts_retries_and_restores_exactly(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    checkpoint = tmp_path / "budget.json"
    provider = _Client([httpx.TimeoutException("timeout"), _Response(200, {"status": 200, "data": []})])
    monkeypatch.setattr(httpx, "Client", lambda **_: provider)
    monkeypatch.setattr("app.finmind.time.sleep", lambda _delay: None)
    budget = FinMindRequestBudget(2, checkpoint)
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=1), request_budget=budget)
    client.fetch("TaiwanStockPrice", "2330", "2026-08-20", "2026-08-20", persist_raw=False)

    assert provider.calls == 2
    assert budget.snapshot() == {"limit": 2, "used": 2, "remaining": 0}
    assert FinMindRequestBudget(2, checkpoint).snapshot() == budget.snapshot()
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["used"] == 2
    with pytest.raises(FinMindError) as exc:
        client.fetch("TaiwanStockPrice", "2330", "2026-08-20", "2026-08-20", persist_raw=False)
    assert exc.value.code == "JOB_REQUEST_BUDGET_EXHAUSTED"
    assert provider.calls == 2


def test_universe_queue_prioritizes_stock_without_data_and_score() -> None:
    with SessionLocal() as db:
        db.query(JobRun).filter(JobRun.dataset == UNIVERSE_BUDGET_REFRESH_DATASET).delete(synchronize_session=False)
        db.add(Stock(stock_id="9999", stock_name="無資料測試", market="上市", is_common_stock=True))
        db.commit()
    try:
        with TestClient(api_app) as client:
            response = client.post("/api/universe/refresh-and-score", params={"source_date": "2026-08-20"})
            assert response.status_code == 202
            job_id = response.json()["job_id"]
        with SessionLocal() as db:
            job = db.get(JobRun, job_id)
            assert job.checkpoint_state["stock_ids"][0] == "9999"
            assert job.checkpoint_state["budget"] == {"limit": 3500, "used": 0, "remaining": 3500}
    finally:
        with SessionLocal() as db:
            db.query(JobRun).filter(JobRun.dataset == UNIVERSE_BUDGET_REFRESH_DATASET).delete(synchronize_session=False)
            db.query(StockRefreshIssue).filter(StockRefreshIssue.stock_id == "9999").delete(synchronize_session=False)
            stock = db.get(Stock, "9999")
            if stock is not None:
                db.delete(stock)
            db.commit()


def test_single_dispatcher_selects_oldest_job_across_both_refresh_types() -> None:
    with SessionLocal() as db:
        older = JobRun(
            dataset=UNIVERSE_BUDGET_REFRESH_DATASET,
            requested_date=date(2026, 8, 20),
            status="QUEUED",
            started_at=datetime(2026, 8, 31, 8, tzinfo=timezone.utc),
            checkpoint_state={"budget": {"limit": 3500, "used": 0, "remaining": 3500}},
        )
        newer = JobRun(
            dataset="favorite_refresh_score",
            requested_date=date(2026, 8, 20),
            status="QUEUED",
            started_at=datetime(2026, 8, 31, 9, tzinfo=timezone.utc),
            checkpoint_state={},
        )
        db.add_all([older, newer])
        db.commit()
        db.refresh(older)
        db.refresh(newer)
        try:
            assert _next_durable_refresh_job(db).id == older.id
            assert _next_durable_refresh_job(db, "favorite_refresh_score").id == newer.id
        finally:
            db.delete(newer)
            db.delete(older)
            db.commit()


def test_two_complete_empty_fetches_are_persisted_and_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with SessionLocal() as db:
        db.add(Stock(stock_id="9998", stock_name="空資料測試", market="上市", is_common_stock=True))
        job = JobRun(
            dataset=UNIVERSE_BUDGET_REFRESH_DATASET,
            requested_date=date(2026, 8, 20),
            requested_start_date=date(2026, 8, 20),
            requested_end_date=date(2026, 8, 20),
            status="QUEUED",
            started_at=datetime.now(timezone.utc),
            stocks_attempted=1,
            checkpoint_state={
                "phase": "queued",
                "stock_ids": ["9998"],
                "cycle_stock_ids": ["9998"],
                "queue_index": 0,
                "budget": {"limit": 2, "used": 0, "remaining": 2},
            },
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    budget = FinMindRequestBudget(2, tmp_path / "job-budget.json")

    class FakeClient:
        settings = SimpleNamespace(source_revision="test", broker_quota_reserve=0)
        request_budget = budget

        def provider_quota(self, *, source_revision: str):
            return {"provider_reported_remaining": 6000, "provider_reported_limit_per_hour": 6000, "plan": "Sponsor"}

    calls = 0

    async def fake_fetch(_db, client, stock_id, _target, **_kwargs):
        nonlocal calls
        calls += 1
        assert stock_id == "9998"
        client.request_budget.reserve()
        return {
            "datasets": {dataset: {"refresh_complete": True} for dataset in FAVORITE_REFRESH_DATASETS},
            "score": {"score": None, "status": "DATA_INSUFFICIENT"},
            "readiness": {"ready": False},
            "fetch_errors": [],
        }

    monkeypatch.setattr(ingestion_module, "fetch_and_score_stock", fake_fetch)
    try:
        with SessionLocal() as db:
            result = asyncio.run(resume_universe_budget_refresh_job(db, FakeClient(), db.get(JobRun, job_id)))
            issue = db.get(StockRefreshIssue, "9998")
            assert result["status"] == "SUCCESS"
            assert result["budget"] == {"limit": 2, "used": 2, "remaining": 0}
            assert calls == 2
            assert issue is not None
            assert issue.no_data_attempts == 2
            assert issue.status == "SKIPPED_AFTER_TWO_NO_DATA"
            assert issue.reason_code == "NO_DATA_AFTER_TWO_FETCHES"
        with TestClient(api_app) as client:
            list_payload = client.get("/api/stocks", params={"search": "9998"}).json()
            detail_payload = client.get("/api/stocks/9998").json()
        assert list_payload["items"][0]["refresh_issue"]["no_data_attempts"] == 2
        assert detail_payload["stock"]["refresh_issue"]["status"] == "SKIPPED_AFTER_TWO_NO_DATA"
    finally:
        with SessionLocal() as db:
            db.query(JobRun).filter(JobRun.id == job_id).delete(synchronize_session=False)
            db.query(StockRefreshIssue).filter(StockRefreshIssue.stock_id == "9998").delete(synchronize_session=False)
            stock = db.get(Stock, "9998")
            if stock is not None:
                db.delete(stock)
            db.commit()
