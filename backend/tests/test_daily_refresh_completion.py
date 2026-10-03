import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.ingestion as ingestion
from app.calendar import closed_market_target_date
from app.finmind import FinMindRequestBudget
from app.models import AccumulationScore, Base, BrokerDaily, HoldingDistribution, JobRun, PriceDaily, Stock, StockRefreshIssue
from app.refresh_completion import daily_refresh_completion
from app.refresh_queue import PARTIAL_SOURCE_SPECS, queue_universe_budget_refresh
from app.scoring import FORMULA_HASH, SCORE_VERSION

TARGET = date(2026, 9, 8)
NOW = datetime(2026, 9, 8, 14, tzinfo=timezone.utc)


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(ingestion, "_now", lambda: NOW)
    monkeypatch.setattr(ingestion, "market_session_state", lambda: {"state": "CLOSED"})
    with Session(engine, expire_on_commit=False) as session:
        session.add_all([Stock(stock_id=sid, stock_name=sid, market="上市", is_common_stock=True) for sid in ["2330", "2408"]])
        session.commit()
        yield session
    engine.dispose()


def certify(db, sid, target=TARGET, score=80, when=NOW):
    for model, dataset in PARTIAL_SOURCE_SPECS.values():
        source_date = target - timedelta(days=(target.weekday() - 4) % 7) if model == HoldingDistribution else target
        row = db.query(model).filter_by(stock_id=sid, source_date=source_date).first()
        if row is None:
            extra = {"holding_shares_level": "400,001-600,000"} if model == HoldingDistribution else {"securities_trader_id": "A"} if model == BrokerDaily else {}
            db.add(model(stock_id=sid, source_date=source_date, source_dataset=dataset, fetched_at=when - timedelta(seconds=1), **extra))
    db.add(AccumulationScore(stock_id=sid, source_date=target, score=score, status="WATCH" if score is not None else "DATA_INSUFFICIENT", score_version=SCORE_VERSION, formula_hash=FORMULA_HASH, calculated_at=when, knowledge_cutoff=when, input_snapshot_hash="a" * 64))
    db.commit()


def exclude(db, sid, attempts=5):
    db.add(StockRefreshIssue(stock_id=sid, no_data_attempts=attempts, status="SKIPPED_AFTER_FIVE_NO_DATA" if attempts >= 5 else "RETRY_PENDING", reason_code="NO_DATA", first_attempt_at=NOW, last_attempt_at=NOW))
    db.commit()


@pytest.mark.parametrize("stamp,target", [
    ("2026-09-08T08:00:00+08:00", "2026-09-07"),
    ("2026-09-08T13:30:00+08:00", "2026-09-08"),
    ("2026-09-08T20:00:00+08:00", "2026-09-08"),
    ("2026-09-09T01:00:00+08:00", "2026-09-08"),
    ("2026-09-12T10:00:00+08:00", "2026-09-11"),
    ("2026-09-25T15:00:00+08:00", "2026-09-24"),
])
def test_target_is_latest_closed_session_not_publication_cutoff(stamp, target):
    assert closed_market_target_date(datetime.fromisoformat(stamp)).isoformat() == target


def test_fallback_null_score_and_four_failures_are_not_complete(db):
    certify(db, "2330", TARGET - timedelta(days=1))
    certify(db, "2408", score=None)
    exclude(db, "2408", 4)
    state = daily_refresh_completion(db, TARGET)
    assert state["pending_count"] == 2 and not state["all_complete"]
    assert state["excluded_count"] == 0


def test_completion_excludes_five_failures_and_no_longer_queues_hourly(db):
    certify(db, "2330")
    exclude(db, "2408")
    first, created = queue_universe_budget_refresh(db, TARGET, automatic=True, now=NOW)
    assert created and first.status == "SUCCESS"
    assert first.checkpoint_state["phase"] == "daily_target_completed"
    assert first.checkpoint_state["budget"]["used"] == 0
    assert first.checkpoint_state["daily_completion"]["excluded_count"] == 1
    for hours in [1, 3, 12]:
        same, created = queue_universe_budget_refresh(db, TARGET, automatic=True, now=NOW + timedelta(hours=hours))
        assert not created and same.id == first.id
    assert db.query(JobRun).count() == 1
    newer, created = queue_universe_budget_refresh(db, TARGET + timedelta(days=1), automatic=True, now=NOW + timedelta(days=1))
    assert created and newer.status == "QUEUED"
    assert newer.checkpoint_state["stock_ids"] == ["2330"]


@pytest.mark.parametrize("mutation", ["newer_failure", "revised_source", "missing_source", "wrong_formula"])
def test_old_success_cannot_hide_invalid_current_state(db, mutation):
    certify(db, "2330")
    exclude(db, "2408")
    assert daily_refresh_completion(db, TARGET)["all_complete"]
    if mutation == "newer_failure":
        certify(db, "2330", score=None, when=NOW + timedelta(seconds=1))
    elif mutation == "revised_source":
        db.query(PriceDaily).filter_by(stock_id="2330").first().fetched_at = NOW + timedelta(seconds=1)
    elif mutation == "missing_source":
        db.query(BrokerDaily).delete()
    else:
        db.query(AccumulationScore).first().formula_hash = "old-formula"
    db.commit()
    assert not daily_refresh_completion(db, TARGET)["all_complete"]


def fake_client(tmp_path, limit=3500):
    return SimpleNamespace(request_budget=FinMindRequestBudget(limit, tmp_path / "budget.json"), settings=SimpleNamespace(broker_quota_reserve=0), provider_quota=lambda **_: {"provider_reported_remaining": 6000})


def result():
    return {"datasets": {name: {"refresh_complete": True, "records_accepted": 1} for name in ingestion.FAVORITE_REFRESH_DATASETS}, "fetch_errors": []}


def test_automatic_fetches_only_pending_and_stops_before_3500(db, monkeypatch, tmp_path):
    certify(db, "2330")
    job, _ = queue_universe_budget_refresh(db, TARGET, automatic=True, now=NOW)
    assert job.checkpoint_state["stock_ids"] == ["2408"]
    client = fake_client(tmp_path)
    calls = []

    async def fetch(db, client, sid, target, **kwargs):
        calls.append(sid)
        assert kwargs["force_refresh"] is False
        assert kwargs["reuse_broker_observations"] is True
        assert kwargs["allow_score_fallback"] is False
        client.request_budget.reserve()
        certify(db, sid, target)
        return result()

    monkeypatch.setattr(ingestion, "fetch_and_score_stock", fetch)
    done = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, client, job))
    assert calls == ["2408"]
    assert done["phase"] == "daily_target_completed" and done["budget"]["used"] == 1
    assert done["daily_completion"]["completed_count"] == 2


def test_budget_exhaustion_continues_next_hour_only_for_remaining_stocks(db, monkeypatch, tmp_path):
    job, _ = queue_universe_budget_refresh(db, TARGET, automatic=True, now=NOW)
    client = fake_client(tmp_path, 1)

    async def fetch(db, client, sid, target, **kwargs):
        client.request_budget.reserve()
        certify(db, sid, target)
        return result()

    monkeypatch.setattr(ingestion, "fetch_and_score_stock", fetch)
    done = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, client, job))
    assert done["phase"] == "budget_completed"
    assert not done["daily_completion"]["all_complete"]
    assert done["daily_completion"]["pending_count"] == 1
    same, created = queue_universe_budget_refresh(db, TARGET, automatic=True, now=NOW)
    assert not created and same.id == job.id
    next_job, created = queue_universe_budget_refresh(db, TARGET, automatic=True, now=NOW + timedelta(hours=1))
    assert created and next_job.id != job.id
    assert next_job.checkpoint_state["stock_ids"] == ["2408"]


def test_resume_moves_old_target_forward_and_resets_source_checkpoints(db, monkeypatch, tmp_path):
    job, _ = queue_universe_budget_refresh(db, TARGET - timedelta(days=1), automatic=True, now=NOW - timedelta(days=1))
    job.status = "WAITING_FOR_PROVIDER"
    job.checkpoint_state = {**job.checkpoint_state, "current_stock_progress": {"stock_id": "2330", "datasets": {"TaiwanStockPrice": {"refresh_complete": True}}}, "next_retry_at": (NOW + timedelta(days=1)).isoformat()}
    db.commit()
    client = fake_client(tmp_path, 2)
    targets = []

    async def fetch(db, client, sid, target, **kwargs):
        assert not kwargs["refreshed_datasets"]
        targets.append(target)
        client.request_budget.reserve()
        certify(db, sid, target)
        return result()

    monkeypatch.setattr(ingestion, "fetch_and_score_stock", fetch)
    done = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, client, job))
    assert targets == [TARGET, TARGET]
    assert done["target_date"] == TARGET
    assert done["phase"] == "daily_target_completed"


def test_manual_priority_can_complete_queued_stock_without_duplicate_fetch(db, monkeypatch, tmp_path):
    certify(db, "2330")
    job, _ = queue_universe_budget_refresh(db, TARGET, automatic=True, now=NOW)
    async def boundary():
        if not daily_refresh_completion(db, TARGET)["all_complete"]:
            certify(db, "2408")
    monkeypatch.setattr(ingestion, "fetch_and_score_stock", lambda *a, **k: pytest.fail("priority request already completed this stock"))
    done = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, fake_client(tmp_path), job, stock_boundary_callback=boundary))
    assert done["phase"] == "daily_target_completed" and done["budget"]["used"] == 0


def test_before_publication_missing_today_does_not_permanently_exclude(db, monkeypatch, tmp_path):
    monkeypatch.setattr(ingestion, "_now", lambda: NOW - timedelta(hours=2))
    job, _ = queue_universe_budget_refresh(db, TARGET, automatic=True, now=NOW)
    async def fetch(db, client, sid, target, **kwargs):
        client.request_budget.reserve()
        return {"datasets": {name: {"refresh_complete": True, "records_accepted": 0} for name in ingestion.FAVORITE_REFRESH_DATASETS}, "fetch_errors": []}
    monkeypatch.setattr(ingestion, "fetch_and_score_stock", fetch)
    done = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, fake_client(tmp_path, 6), job))
    assert not done["daily_completion"]["all_complete"]
    assert db.query(StockRefreshIssue).count() == 0


def test_empty_universe_is_not_falsely_complete_and_all_excluded_is_done(db):
    for sid in ["2330", "2408"]:
        exclude(db, sid)
    assert daily_refresh_completion(db, TARGET)["all_complete"]
    assert daily_refresh_completion(db, TARGET)["eligible_count"] == 0
    db.query(StockRefreshIssue).delete()
    db.query(Stock).delete()
    db.commit()
    assert not daily_refresh_completion(db, TARGET)["all_complete"]
