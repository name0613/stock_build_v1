import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.ingestion as ingestion
import app.manual_refresh as manual
import app.worker as worker
from app.db import SessionLocal
from app.finmind import FinMindRequestBudget
from app.main import app
from app.models import JobRun


@pytest.fixture(autouse=True)
def clean_jobs(monkeypatch):
    datasets = [manual.MANUAL_STOCK_REFRESH_DATASET, ingestion.UNIVERSE_BUDGET_REFRESH_DATASET, ingestion.FAVORITE_REFRESH_DATASET]
    with SessionLocal() as db:
        db.query(JobRun).filter(JobRun.dataset.in_(datasets)).delete(synchronize_session=False)
        db.commit()
    monkeypatch.setattr(worker, "_heartbeat", lambda **_: None)
    yield
    with SessionLocal() as db:
        db.query(JobRun).filter(JobRun.dataset.in_(datasets)).delete(synchronize_session=False)
        db.commit()


def client_with_quota(remaining=6000):
    return SimpleNamespace(settings=SimpleNamespace(source_revision="test", broker_quota_reserve=0), provider_quota=lambda **_: {"provider_reported_remaining": remaining})


def test_api_queues_while_batch_child_running_and_reuses_same_stock(monkeypatch):
    monkeypatch.setattr(manual, "fetch_and_score_stock", lambda *a, **k: pytest.fail("API must never fetch"))
    with SessionLocal() as db:
        child = JobRun(dataset=ingestion.TARGETED_STOCK_SYNC_DATASET, status="RUNNING", started_at=datetime.now(timezone.utc), checkpoint_state={"stock_id": "2317"})
        db.add(child)
        db.commit()
        child_id = child.id
    try:
        with TestClient(app) as api:
            first = api.post("/api/stocks/2330/fetch-and-score", params={"source_date": "2026-08-20"})
            assert first.status_code == 202 and first.json()["status"] == "QUEUED"
            duplicate = api.post("/api/stocks/2330/fetch-and-score")
            assert duplicate.status_code == 202
            assert duplicate.json()["job_id"] == first.json()["job_id"]
            other = api.post("/api/stocks/1101/fetch-and-score")
            assert other.status_code == 202
            assert api.get("/api/stocks/2330/fetch-and-score").json()["job_id"] == first.json()["job_id"]
            assert api.get(f"/api/stocks/1101/fetch-and-score?job_id={first.json()['job_id']}").status_code == 404
    finally:
        with SessionLocal() as db:
            db.delete(db.get(JobRun, child_id))
            db.commit()


def test_concurrent_duplicate_clicks_create_one_job():
    def enqueue(_):
        with SessionLocal() as db:
            return manual.queue_manual_stock_refresh(db, "2330", date(2026, 8, 20)).id
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(enqueue, range(4)))
    assert len(set(ids)) == 1


@pytest.mark.parametrize("kind", ["universe", "favorites"])
def test_manual_handoff_occurs_between_stocks_under_same_lock(monkeypatch, tmp_path, kind):
    events = []
    client = client_with_quota()
    client.request_budget = FinMindRequestBudget(2, tmp_path / "budget.json")
    monkeypatch.setattr(worker, "FinMindClient", lambda *a, **k: client)

    async def manual_fetch(db, _client, stock_id, target, *, job, **kwargs):
        assert worker._provider_work_lock.locked()
        events.append(f"manual:{stock_id}")
        job.status = "SUCCESS"
        job.finished_at = datetime.now(timezone.utc)
        job.checkpoint_state = {**job.checkpoint_state, "phase": "completed", "score": {"score": 80}, "fetch_errors": []}
        db.commit()
        return {"fetch_errors": [], "datasets": {}}

    async def batch_fetch(db, provider, stock_id, target, **kwargs):
        assert worker._provider_work_lock.locked()
        events.append(f"batch-start:{stock_id}")
        if stock_id == "2330":
            with SessionLocal() as queue_db:
                manual.queue_manual_stock_refresh(queue_db, "1101", date(2026, 8, 20))
                manual.queue_manual_stock_refresh(queue_db, "1102", date(2026, 8, 20))
            assert events == ["batch-start:2330"]
        provider.request_budget.reserve()
        events.append(f"batch-end:{stock_id}")
        return {"datasets": {name: {"refresh_complete": True, "records_accepted": 1} for name in ingestion.FAVORITE_REFRESH_DATASETS}, "fetch_errors": []}

    monkeypatch.setattr(manual, "fetch_and_score_stock", manual_fetch)
    monkeypatch.setattr(ingestion, "fetch_and_score_stock", batch_fetch)
    with SessionLocal() as db:
        job = JobRun(dataset=ingestion.UNIVERSE_BUDGET_REFRESH_DATASET if kind == "universe" else ingestion.FAVORITE_REFRESH_DATASET, status="QUEUED", started_at=datetime.now(timezone.utc), requested_end_date=date(2026, 8, 20), checkpoint_state={"stock_ids": ["2330", "2317"], "cycle_stock_ids": ["2330", "2317"], "budget": {"used": 0, "limit": 2}})
        db.add(job)
        db.commit()
        runner = ingestion.resume_universe_budget_refresh_job if kind == "universe" else ingestion.resume_favorite_refresh_job
        with worker._provider_work_lock:
            result = asyncio.run(runner(db, client, job, stock_boundary_callback=worker._drain_manual_refresh_jobs))
        assert result["status"] == "SUCCESS"
        assert client.request_budget.snapshot()["used"] == 2
    assert events == ["batch-start:2330", "batch-end:2330", "manual:1101", "manual:1102", "batch-start:2317", "batch-end:2317"]


def test_dispatcher_runs_manual_first_and_never_while_lock_busy(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "FinMindClient", lambda *a, **k: client_with_quota())

    async def fetch(db, _client, stock_id, target, *, job, **kwargs):
        assert worker._provider_work_lock.locked()
        calls.append(stock_id)
        job.status = "SUCCESS"
        db.commit()
        return {"datasets": {}, "fetch_errors": []}

    monkeypatch.setattr(manual, "fetch_and_score_stock", fetch)
    with SessionLocal() as db:
        batch = JobRun(dataset=ingestion.UNIVERSE_BUDGET_REFRESH_DATASET, status="QUEUED", started_at=datetime.now(timezone.utc), checkpoint_state={})
        db.add(batch)
        db.commit()
        job = manual.queue_manual_stock_refresh(db, "2330", date(2026, 8, 20))
        assert worker._next_durable_refresh_job(db).id == job.id
    with worker._provider_work_lock:
        worker.run_durable_refresh()
        assert calls == []
    worker.run_durable_refresh()
    assert calls == ["2330"]


def test_quota_wait_restart_and_resume_preserve_completed_sources(monkeypatch):
    calls = []

    async def fetch(db, _client, stock_id, target, *, job, refreshed_datasets, **kwargs):
        calls.append(refreshed_datasets)
        job.status = "SUCCESS"
        db.commit()
        return {"datasets": {}, "fetch_errors": []}

    monkeypatch.setattr(manual, "fetch_and_score_stock", fetch)
    with SessionLocal() as db:
        job = manual.queue_manual_stock_refresh(db, "2330", date(2026, 8, 20))
        job.checkpoint_state = {**job.checkpoint_state, "datasets": {"TaiwanStockPrice": {"refresh_complete": True}}}
        db.commit()
        result = asyncio.run(manual.resume_manual_stock_refresh(db, client_with_quota(0), job))
        assert result["status"] == "WAITING_FOR_QUOTA" and calls == []
        assert worker._next_durable_refresh_job(db) is None
        job.status = "RUNNING"
        db.commit()
        worker._reconcile_interrupted_jobs(db)
        assert job.status == "QUEUED"
        result = asyncio.run(manual.resume_manual_stock_refresh(db, client_with_quota(), job))
        assert calls == [{"TaiwanStockPrice"}]
        assert result["status"] == "SUCCESS"


@pytest.mark.parametrize("codes,expected", [
    (["PARTIAL_RESPONSE_UNVERIFIED"], "DATA_INSUFFICIENT"),
    (["PARTIAL_RESPONSE_UNVERIFIED", "NETWORK_ERROR"], "WAITING_FOR_PROVIDER"),
    (["INCOMPLETE_PROVIDER_COVERAGE"], "WAITING_FOR_PROVIDER"),
])
def test_manual_partial_gap_finishes_without_hiding_other_failures(monkeypatch, codes, expected):
    async def fetch(db, _client, stock_id, target, *, job, **kwargs):
        result = {"status": "DATA_INSUFFICIENT", "fetch_errors": [],
                  "datasets": {"TaiwanStockInstitutionalInvestorsBuySellWide": {
                      "refresh_complete": False, "failure_codes": codes, "retryable_pending": 1, "records_accepted": 19}},
                  "score": {"score": None}, "target_readiness": {"ready": False}}
        job.checkpoint_state = {**job.checkpoint_state, **result}
        db.commit()
        return result

    monkeypatch.setattr(manual, "fetch_and_score_stock", fetch)
    with SessionLocal() as db:
        job = manual.queue_manual_stock_refresh(db, "2330", date(2026, 8, 20))
        result = asyncio.run(manual.resume_manual_stock_refresh(db, client_with_quota(), job))
        assert result["status"] == expected
        assert result["score"]["score"] is None
        assert result["datasets"]["TaiwanStockInstitutionalInvestorsBuySellWide"]["refresh_complete"] is False


def test_manual_failure_does_not_leave_running_job_or_block_next(monkeypatch):
    calls = []

    async def fetch(db, _client, stock_id, target, *, job, **kwargs):
        calls.append(stock_id)
        if stock_id == "2330":
            raise RuntimeError("test failure")
        job.status = "SUCCESS"
        db.commit()
        return {"fetch_errors": []}

    monkeypatch.setattr(manual, "fetch_and_score_stock", fetch)
    monkeypatch.setattr(worker, "FinMindClient", lambda *a, **k: client_with_quota())
    with SessionLocal() as db:
        first = manual.queue_manual_stock_refresh(db, "2330", date(2026, 8, 20)).id
        manual.queue_manual_stock_refresh(db, "2317", date(2026, 8, 20))
    worker.run_durable_refresh()
    assert calls == ["2330", "2317"]
    with SessionLocal() as db:
        assert db.get(JobRun, first).status == "FAILED"


def test_restart_with_pending_requests_starts_dispatcher_before_nightly_catchup(monkeypatch):
    registered = []

    class Scheduler:
        running = False

        def __init__(self, **kwargs):
            pass

        def add_job(self, *args, **kwargs):
            registered.append(kwargs["id"])

        def add_listener(self, *args):
            pass

        def start(self):
            raise KeyboardInterrupt

        def shutdown(self, **kwargs):
            pass

    with SessionLocal() as db:
        manual.queue_manual_stock_refresh(db, "2330", date(2026, 8, 20))
    monkeypatch.setattr(worker, "_startup_catch_up_allowed", lambda: True)
    monkeypatch.setattr(worker, "run_catch_up", lambda: pytest.fail("must restore queued work first"))
    monkeypatch.setattr(worker, "start_health_server", lambda *args: None)
    monkeypatch.setattr(worker, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(worker, "BlockingScheduler", Scheduler)
    monkeypatch.setattr(worker, "_scheduler_runtime", None)
    worker.main()
    assert "durable-refresh-resume" in registered
