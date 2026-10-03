"""A calendar date must never become a daily-data target on a closed session."""
import asyncio
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.finmind as finmind
import app.ingestion as ingestion
from app.calendar import closed_market_target_date, completed_source_end_date
from app.config import Settings
from app.db import get_db
from app.main import app
from app.manual_refresh import queue_manual_stock_refresh
from app.models import Base, JobRun, Stock
from app.refresh_queue import queue_universe_budget_refresh


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.add(Stock(stock_id="2330", stock_name="Test", market="上市", is_common_stock=True, is_favorite=True))
        session.commit()
        yield session
    engine.dispose()


@pytest.mark.parametrize("stamp,closed,published", [
    ("2026-10-03T10:00:00+08:00", "2026-10-02", "2026-10-02"),
    ("2026-10-04T23:00:00+08:00", "2026-10-02", "2026-10-02"),
    ("2026-10-05T08:30:00+08:00", "2026-10-02", "2026-10-02"),
    ("2026-10-05T13:30:00+08:00", "2026-10-05", "2026-10-02"),
    ("2026-10-05T21:00:00+08:00", "2026-10-05", "2026-10-05"),
    ("2026-09-28T23:00:00+08:00", "2026-09-24", "2026-09-24"),
    ("2026-10-11T23:00:00+08:00", "2026-10-08", "2026-10-08"),
    ("2026-02-22T23:00:00+08:00", "2026-02-11", "2026-02-11"),
])
def test_closed_day_and_publication_targets(stamp, closed, published):
    now = datetime.fromisoformat(stamp)
    assert closed_market_target_date(now).isoformat() == closed
    assert completed_source_end_date(now).isoformat() == published


@pytest.mark.parametrize("path", [
    "/api/stocks/2330/fetch-and-score",
    "/api/favorites/fetch-and-score",
    "/api/universe/refresh-and-score",
])
def test_api_queues_the_previous_trading_day(db, path):
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as api:
            response = api.post(path, params={"source_date": "2026-09-28"})
        assert response.status_code == 202, response.text
        assert response.json()["target_date"] == "2026-09-24"
        job = db.get(JobRun, response.json()["job_id"])
        assert job.requested_end_date == date(2026, 9, 24)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("queue", [queue_manual_stock_refresh, queue_universe_budget_refresh])
def test_direct_queue_call_normalizes_weekend(db, queue):
    if queue is queue_manual_stock_refresh:
        job = queue(db, "2330", date(2026, 10, 4))
    else:
        job, _ = queue(db, date(2026, 10, 4))
    assert job.requested_end_date == date(2026, 10, 2)
    assert job.checkpoint_state["target_date"] == "2026-10-02"


class RecordingClient:
    def __init__(self):
        self.calls = []

    def fetch(self, dataset, **kwargs):
        assert dataset == "TaiwanStockInfo"
        return [{"stock_id": "2330", "stock_name": "Test", "type": "twse", "security_type": "股票"}], {}

    async def fetch_stocks_dataset(self, stock_ids, dataset, start, end, **kwargs):
        self.calls.append((dataset, start, end))
        return {"success": 0, "physical_requests": 1, "retryable_pending": 1, "fatal_code": "QUOTA_EXHAUSTED"}

    async def fetch_broker_stocks(self, stock_ids, start, end, **kwargs):
        return await self.fetch_stocks_dataset(stock_ids, "TaiwanStockTradingDailyReport", start, end, **kwargs)


@pytest.mark.parametrize("operation", ["catch_up", "intraday_sync", "fetch_and_score_stock"])
@pytest.mark.parametrize("explicit", [False, True])
def test_provider_requests_and_job_dates_use_a_real_session(db, monkeypatch, operation, explicit):
    monkeypatch.setattr(ingestion, "_now", lambda: datetime.fromisoformat("2026-10-04T23:00:00+08:00"))
    client = RecordingClient()
    requested = date(2026, 10, 4) if explicit else None
    if operation == "fetch_and_score_stock":
        asyncio.run(ingestion.fetch_and_score_stock(db, client, "2330", requested))
    else:
        asyncio.run(getattr(ingestion, operation)(db, client, end_date=requested))
    assert client.calls
    assert {call[2] for call in client.calls} == {"2026-10-02"}
    jobs = db.scalars(select(JobRun)).all()
    assert jobs and all(job.requested_end_date == date(2026, 10, 2) for job in jobs)


def test_legacy_manual_job_revalidates_its_corrected_target(db, monkeypatch):
    job = JobRun(dataset="manual_stock_refresh_score", status="QUEUED", started_at=datetime.fromisoformat("2026-09-28T23:00:00+08:00"), requested_end_date=date(2026, 9, 28), checkpoint_state={"stock_id": "2330"})
    db.add(job)
    db.commit()
    client = RecordingClient()
    asyncio.run(ingestion.fetch_and_score_stock(db, client, "2330", job.requested_end_date, job=job,
        refreshed_datasets=set(ingestion.FAVORITE_REFRESH_DATASETS)))
    assert client.calls  # Old holiday checkpoints cannot bypass revalidation.
    assert job.requested_end_date == date(2026, 9, 24)
    assert job.checkpoint_state["target_date"] == "2026-09-24"


@pytest.mark.parametrize("dataset", ["TaiwanStockPrice", "TaiwanStockTradingDailyReport", "TaiwanStockTradingDailyReportSecIdAgg"])
def test_capability_probe_uses_published_trading_day(tmp_path, monkeypatch, dataset):
    monkeypatch.setattr(finmind, "completed_source_end_date", lambda: date(2026, 9, 24))
    client = finmind.FinMindClient(Settings(raw_root=tmp_path))
    calls = []
    def fetch(dataset, **kwargs):
        calls.append(kwargs)
        return [], {}
    monkeypatch.setattr(client, "_fetch_capability_probe", fetch)
    client.probe(dataset)
    assert calls[0]["end_date"] == "2026-09-24"
    if "TradingDailyReport" in dataset:
        assert calls[0]["start_date"] == "2026-09-24"
