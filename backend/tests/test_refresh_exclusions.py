import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.ingestion as ingestion
import app.manual_refresh as manual
import app.refresh_exclusions as exclusions
import app.worker as worker
from app.db import get_db
from app.main import app
from app.models import (AccumulationScore, Base, BrokerDaily, CapitalAwareScore, JobRun,
                        PriceDaily, RefreshExclusionRecovery, Stock, StockRefreshIssue)
from app.refresh_completion import daily_refresh_completion
from app.refresh_queue import _universe_budget_queue, queue_universe_budget_refresh
from app.finmind import FinMindClient, FinMindError, FinMindRequestBudget
from app.config import Settings
from app.scoring import BROKER_ROW_CONTRACT_VERSION
from test_per_stock_scoring_gate import _seed_complete_sources, END

NOW = datetime(2026, 9, 8, 14, tzinfo=timezone.utc)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'exclusions.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    yield factory
    engine.dispose()


@pytest.fixture
def db(sessions):
    with sessions() as session:
        yield session


@pytest.fixture
def api(sessions, monkeypatch):
    def override():
        with sessions() as session:
            yield session
    app.dependency_overrides[get_db] = override
    monkeypatch.setattr(FinMindClient, "fetch", lambda *a, **kw: pytest.fail("listing/POST must not fetch"))
    monkeypatch.setattr(FinMindClient, "provider_quota", lambda *a, **kw: pytest.fail("listing/POST must not probe"))
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.pop(get_db)


def exclude(db, sid="9001", *, attempts=5, partial=False, status="legacy_skipped", details=None):
    if db.get(Stock, sid) is None:
        db.add(Stock(stock_id=sid, stock_name=f"測試{sid}", market="上市", is_common_stock=True))
    db.add(StockRefreshIssue(stock_id=sid, no_data_attempts=attempts, status=status,
        reason_code="INCOMPLETE_AFTER_TWO_FETCHES" if partial else "NO_DATA_AFTER_TWO_FETCHES",
        first_attempt_at=NOW - timedelta(days=10), last_attempt_at=NOW,
        details=details or {}))
    db.commit()


def client(remaining=6000):
    return SimpleNamespace(settings=SimpleNamespace(source_revision="test", broker_quota_reserve=0),
                           provider_quota=lambda **kw: {"provider_reported_remaining": remaining})


def finish(db, job, status="DATA_INSUFFICIENT"):
    ingestion._job_finish(db, job, status, checkpoint_state={**job.checkpoint_state, "phase": "completed"})


def test_paginated_api_uses_counter_not_status_and_never_fetches(db, api):
    for index in range(257):
        sid = str(7000 + index)
        exclude(db, sid, partial=index % 2 == 1, status="RECOVERED" if index % 2 else "SKIPPED_AFTER_TWO_NO_DATA",
                details={"incomplete_datasets": ["TaiwanStockPrice"]} if index == 1 else None)
        db.get(Stock, sid).market = "上櫃" if index % 2 else "上市"
    exclude(db, "9999", attempts=4, status="SKIPPED_AFTER_FIVE_NO_DATA")
    db.commit()
    ids = []
    for page in range(1, 7):
        response = api.get("/api/refresh-exclusions", params={"page": page}).json()
        assert response["total"] == response["filtered_total"] == 257
        ids.extend(row["stock_id"] for row in response["items"])
    assert len(set(ids)) == 257 and "9999" not in ids
    assert set(ids) == ingestion.skipped_refresh_stock_ids(db)
    first = api.get("/api/refresh-exclusions").json()["items"]
    assert first[0]["reason"] == "NO_DATA" and first[0]["missing_sources"] is None
    assert first[1]["reason"] == "INCOMPLETE" and first[1]["missing_sources"] == ["TaiwanStockPrice"]
    search = api.get("/api/refresh-exclusions", params={"search": "測試7001", "market": "上櫃"}).json()
    assert search["total"] == 257 and search["filtered_total"] == 1
    assert api.get("/api/refresh-exclusions?search=%25").json()["filtered_total"] == 0
    assert api.get("/api/refresh-exclusions?market=上櫃").json()["filtered_total"] == 128
    assert api.get("/api/refresh-exclusions?page=0").status_code == 422


def test_validation_enqueue_duplicate_and_merge_intent(db, api):
    exclude(db)
    first = api.post("/api/stocks/9001/fetch-and-score?source_date=2026-08-27").json()
    assert api.post("/api/refresh-exclusions/9001/recover?source_date=bad").status_code == 422
    assert db.query(RefreshExclusionRecovery).count() == 0
    assert api.post("/api/refresh-exclusions/0000/recover").status_code == 404
    result = api.post("/api/refresh-exclusions/9001/recover").json()
    assert result["job_id"] == first["job_id"] and result["recovery"]["released_at"] is None
    assert api.post("/api/refresh-exclusions/9001/recover").status_code == 202
    assert db.query(JobRun).count() == db.query(RefreshExclusionRecovery).count() == 1
    assert db.get(StockRefreshIssue, "9001", populate_existing=True).no_data_attempts == 5
    listed = api.get("/api/refresh-exclusions").json()
    assert listed["items"][0]["job"]["job_id"] == result["job_id"]
    assert listed["active_jobs"][0]["job_id"] == result["job_id"]
    finish(db, db.get(JobRun, first["job_id"]))
    assert api.post("/api/refresh-exclusions/9001/recover").json()["job_id"] == first["job_id"]
    receipt = api.get(f"/api/stocks/9001/fetch-and-score?job_id={first['job_id']}").json()
    assert receipt["recovery"]["automatic_refresh_eligibility"] == "RESTORED"
    assert api.get("/api/refresh-exclusions").json()["recent_results"][0]["job_id"] == first["job_id"]


def test_concurrent_recovery_posts_make_one_job(db, sessions):
    exclude(db)
    def enqueue(_):
        with sessions() as session:
            return manual.queue_manual_stock_refresh(session, "9001", END, recover_exclusion=True).id
    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(enqueue, range(12)))
    assert len(set(ids)) == 1
    assert db.query(RefreshExclusionRecovery).count() == db.query(JobRun).count() == 1


@pytest.mark.parametrize("code", ["QUOTA_EXHAUSTED", "TIMEOUT"])
def test_wait_and_restart_preserve_exclusion_and_resume_checkpoints(db, sessions, monkeypatch, code):
    exclude(db)
    job = manual.queue_manual_stock_refresh(db, "9001", END, recover_exclusion=True)
    async def fetch(db, provider, sid, target, **kw):
        assert kw["defer_finish"] and kw["reuse_broker_observations"]
        kw["job"].checkpoint_state = {**kw["job"].checkpoint_state, "datasets": {"TaiwanStockPrice": {"refresh_complete": True}}}
        db.commit()
        return {"datasets": {"broker": {"failure_codes": [code]}}, "fetch_errors": [{"error_code": code}]}
    monkeypatch.setattr(manual, "fetch_and_score_stock", fetch)
    result = asyncio.run(manual.resume_manual_stock_refresh(db, client(), job))
    assert result["status"] == ("WAITING_FOR_QUOTA" if code == "QUOTA_EXHAUSTED" else "WAITING_FOR_PROVIDER")
    assert job.finished_at is None and "9001" in ingestion.skipped_refresh_stock_ids(db)
    assert db.get(RefreshExclusionRecovery, job.id).released_at is None
    with sessions() as restarted:
        resumed = restarted.get(JobRun, job.id)
        resumed.status = "RUNNING"
        restarted.commit()
        worker._reconcile_interrupted_jobs(restarted)
        async def complete(db, provider, sid, target, **kw):
            assert kw["refreshed_datasets"] == {"TaiwanStockPrice"}
            return {"status": "DATA_INSUFFICIENT", "datasets": {}, "fetch_errors": []}
        monkeypatch.setattr(manual, "fetch_and_score_stock", complete)
        result = asyncio.run(manual.resume_manual_stock_refresh(restarted, client(), resumed))
        assert result["recovery"]["released_at"] and result["status"] == "DATA_INSUFFICIENT"


def test_quota_precheck_keeps_exclusion(db):
    exclude(db)
    job = manual.queue_manual_stock_refresh(db, "9001", END, recover_exclusion=True)
    result = asyncio.run(manual.resume_manual_stock_refresh(db, client(0), job))
    assert result["status"] == "WAITING_FOR_QUOTA"
    assert "9001" in ingestion.skipped_refresh_stock_ids(db)


@pytest.mark.parametrize("failure", ["ACCESS_DENIED", "unexpected"])
def test_worker_final_failure_releases_without_numeric_score(db, monkeypatch, failure):
    exclude(db, partial=True)
    job = manual.queue_manual_stock_refresh(db, "9001", END, recover_exclusion=True)
    async def fail(*args, **kwargs):
        if failure == "unexpected":
            raise RuntimeError("test")
        raise FinMindError(failure, "test")
    monkeypatch.setattr(manual, "fetch_and_score_stock", fail)
    monkeypatch.setattr(worker, "FinMindClient", lambda *a, **kw: client())
    monkeypatch.setattr(worker, "_heartbeat", lambda **kw: None)
    asyncio.run(worker._execute_manual_refresh(db, job))
    assert job.status == "FAILED" and "9001" not in ingestion.skipped_refresh_stock_ids(db)
    recovery = db.get(RefreshExclusionRecovery, job.id)
    assert recovery.released_at and recovery.result["status"] == "FAILED"
    assert recovery.previous_issue["no_data_attempts"] == 5
    assert recovery.previous_issue["reason_code"].startswith("INCOMPLETE")


def test_real_fetch_scores_immediately_reuses_data_and_preserves_history(db):
    exclude(db)
    _seed_complete_sources(db, "9001")
    history = ingestion.calculate_stock_features_and_score(db, "9001", END)
    historical_id, historical_score = history.id, history.score
    db.query(BrokerDaily).filter(BrokerDaily.source_date == END).delete()
    db.commit()
    price_count = db.query(PriceDaily).count()
    provider = client()
    async def broker(sids, start, end, **kwargs):
        assert len(kwargs["reusable_observations"]) == 19
        assert kwargs["retry_deferred"] and not kwargs.get("force_refresh")
        kwargs["record_sink"]([{"stock_id": "9001", "date": END.isoformat(), "securities_trader_id": "A", "buy": 100, "sell": 10,
            "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION}])
        return {"success": 20, "physical_requests": 1, "remaining_pending_after_run": 0}
    async def source(*args, **kw):
        pytest.fail("valid local sources must be reused")
    provider.fetch_broker_stocks = broker
    provider.fetch_stocks_dataset = source
    job = manual.queue_manual_stock_refresh(db, "9001", END, recover_exclusion=True)
    result = asyncio.run(manual.resume_manual_stock_refresh(db, provider, job))
    assert result["status"] == "SUCCESS" and result["score"]["score"] is not None
    assert db.query(CapitalAwareScore).count() >= 2
    assert result["evaluated_source_date"] == END.isoformat()
    assert result["recovery"]["released_at"] and not ingestion.skipped_refresh_stock_ids(db)
    assert db.get(StockRefreshIssue, "9001").no_data_attempts == 0
    assert db.query(PriceDaily).count() == price_count
    assert db.get(AccumulationScore, historical_id).score == historical_score


@pytest.mark.parametrize("fallback", [False, True])
def test_real_insufficient_or_fallback_is_terminal_and_keeps_target_truth(db, fallback):
    exclude(db)
    if fallback:
        _seed_complete_sources(db, "9001")
    provider = client()
    async def empty(*args, **kwargs):
        return {"success": 0, "physical_requests": 1, "failure_codes": ["PARTIAL_OBSERVATION_COVERAGE"], "retryable_pending": 1}
    provider.fetch_stocks_dataset = provider.fetch_broker_stocks = empty
    target = END + timedelta(days=1)
    job = manual.queue_manual_stock_refresh(db, "9001", target, recover_exclusion=True)
    result = asyncio.run(manual.resume_manual_stock_refresh(db, provider, job))
    assert result["status"] == ("SUCCESS" if fallback else "DATA_INSUFFICIENT")
    assert result["fallback_applied"] is fallback
    assert result["target_readiness"]["missing_reasons"]
    if fallback:
        assert result["evaluated_source_date"] == END.isoformat()
    else:
        assert result["score"]["score"] is None
    assert result["recovery"]["released_at"]
    assert daily_refresh_completion(db, target)["pending_stock_ids"] == ["9001"]


def test_reset_recounts_and_old_job_replay_cannot_reset_again(db):
    exclude(db)
    job = manual.queue_manual_stock_refresh(db, "9001", END, recover_exclusion=True)
    finish(db, job)
    released = db.get(RefreshExclusionRecovery, job.id).released_at
    for count in range(1, 6):
        issue = ingestion._record_no_data_attempt(db, "9001", None, {})
        db.commit()
        assert issue.no_data_attempts == count
        finish(db, job)  # replay of the old completion boundary
        assert db.get(StockRefreshIssue, "9001").no_data_attempts == count
        assert ("9001" in ingestion.skipped_refresh_stock_ids(db)) == (count == 5)
    assert db.get(RefreshExclusionRecovery, job.id).released_at == released
    finished_at = job.finished_at
    ingestion._job_finish(db, job, "FAILED", checkpoint_state={"phase": "failed"})
    assert job.status == "DATA_INSUFFICIENT" and job.finished_at == finished_at
    assert job.checkpoint_state["phase"] == "completed"
    assert db.get(RefreshExclusionRecovery, job.id).result["status"] == job.status
    again = manual.queue_manual_stock_refresh(db, "9001", END, recover_exclusion=True)
    assert again.id != job.id
    assert db.query(RefreshExclusionRecovery).count() == 2


def test_general_recovery_and_general_manual_job_keep_cumulative_semantics(db):
    exclude(db, attempts=4)
    ingestion._mark_refresh_recovered(db, "9001")
    db.commit()
    assert db.get(StockRefreshIssue, "9001").no_data_attempts == 4
    ingestion._record_no_data_attempt(db, "9001", None, {})
    db.commit()
    ingestion._mark_refresh_recovered(db, "9001")
    db.commit()
    job = manual.queue_manual_stock_refresh(db, "9001", END)
    finish(db, job, "SUCCESS")
    assert "9001" in ingestion.skipped_refresh_stock_ids(db)
    assert db.query(RefreshExclusionRecovery).count() == 0


def test_completion_crash_rolls_back_both_job_and_release_then_restarts(db, sessions, monkeypatch):
    exclude(db)
    job = manual.queue_manual_stock_refresh(db, "9001", END, recover_exclusion=True)
    job.status = "RUNNING"
    job.checkpoint_state = {**job.checkpoint_state, "phase": "ready_to_finalize", "datasets": {"TaiwanStockPrice": {"refresh_complete": True}}}
    db.commit()
    real_release = exclusions.release_exclusion
    class Crash(BaseException):
        pass
    def crash(session, job):
        real_release(session, job)
        session.flush()
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(exclusions, "release_exclusion", crash)
        with pytest.raises(Crash):
            finish(db, job)
    with sessions() as restarted:
        resumed = restarted.get(JobRun, job.id)
        assert resumed.status == "RUNNING" and resumed.finished_at is None
        assert restarted.get(RefreshExclusionRecovery, job.id).released_at is None
        assert restarted.get(StockRefreshIssue, "9001").no_data_attempts == 5
        worker._reconcile_interrupted_jobs(restarted)
        assert resumed.status == "QUEUED"
        finish(restarted, resumed)
        assert resumed.status == "DATA_INSUFFICIENT"
        assert restarted.get(RefreshExclusionRecovery, job.id).released_at
        assert restarted.get(StockRefreshIssue, "9001").no_data_attempts == 0


def test_running_job_intent_merge_survives_worker_stale_checkpoint(db, sessions):
    exclude(db)
    job = manual.queue_manual_stock_refresh(db, "9001", END)
    job.status = "RUNNING"
    db.commit()
    stale = dict(job.checkpoint_state)
    with sessions() as api_db:
        assert manual.queue_manual_stock_refresh(api_db, "9001", END, recover_exclusion=True).id == job.id
    ingestion._job_finish(db, job, "FAILED", checkpoint_state={**stale, "phase": "failed"})
    assert db.get(RefreshExclusionRecovery, job.id).released_at
    assert "9001" not in ingestion.skipped_refresh_stock_ids(db)


def test_completed_daily_schedule_reopens_same_hour_without_rewriting_history(db, sessions):
    exclude(db)
    old, _ = queue_universe_budget_refresh(db, END, automatic=True, now=NOW)
    old_state = dict(old.checkpoint_state)
    assert old_state["phase"] == "daily_target_completed"
    # Deliberately retain an issue identity in the old batch session.
    stale_issue = db.get(StockRefreshIssue, "9001")
    with sessions() as manual_db:
        job = manual.queue_manual_stock_refresh(manual_db, "9001", END, recover_exclusion=True)
        finish(manual_db, job)
    assert stale_issue.no_data_attempts == 5
    assert _universe_budget_queue(db)[0] == ["9001"]
    state = daily_refresh_completion(db, END)
    assert state["excluded_count"] == 0 and state["pending_count"] == 1
    next_job, created = queue_universe_budget_refresh(db, END, automatic=True, now=NOW)
    assert created and next_job.id != old.id and next_job.checkpoint_state["stock_ids"] == ["9001"]
    assert old.checkpoint_state == old_state
    fresh = ingestion._record_no_data_attempt(db, "9001", next_job.id, {})
    assert fresh.no_data_attempts == 1  # cross-session old 5 must not leak back


def test_broker_retries_old_empty_only_and_reuses_valid_observations(tmp_path, monkeypatch):
    provider = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, broker_concurrency=1))
    calls = []
    def empty(dataset, sid, start, end):
        calls.append(start)
        return [], {"empty_is_valid": True, "empty_reason": "no_provider_observation", "attempt": 1}
    monkeypatch.setattr(provider, "fetch", empty)
    asyncio.run(provider.fetch_broker_stocks(["9001"], "2026-08-26", "2026-08-27"))
    calls.clear()
    result = asyncio.run(provider.fetch_broker_stocks(["9001"], "2026-08-26", "2026-08-27",
        retry_deferred=True, reusable_observations={"9001:2026-08-26"}))
    assert calls == ["2026-08-27"] and result["physical_requests"] == 1


@pytest.mark.parametrize("automatic", [False, True])
def test_running_batch_discovers_restored_stock_at_boundary(db, sessions, tmp_path, monkeypatch, automatic):
    exclude(db)
    db.add(Stock(stock_id="9002", stock_name="valid", market="上市", is_common_stock=True))
    db.commit()
    job, _ = queue_universe_budget_refresh(db, END, automatic=automatic, now=NOW)
    assert job.checkpoint_state["stock_ids"] == ["9002"]
    stale_issue = db.get(StockRefreshIssue, "9001")
    provider = client()
    provider.request_budget = FinMindRequestBudget(2, tmp_path / "budget.json")
    restored = False
    async def boundary():
        nonlocal restored
        if restored:
            return
        restored = True
        with sessions() as other:
            recovery = manual.queue_manual_stock_refresh(other, "9001", END, recover_exclusion=True)
            finish(other, recovery)
    calls = []
    async def fetch(db, provider, sid, target, **kwargs):
        calls.append(sid)
        provider.request_budget.reserve()
        return {"datasets": {name: {"refresh_complete": True, "records_accepted": 0} for name in ingestion.FAVORITE_REFRESH_DATASETS}, "fetch_errors": []}
    monkeypatch.setattr(ingestion, "fetch_and_score_stock", fetch)
    monkeypatch.setattr(ingestion, "closed_market_target_date", lambda *a: END)
    monkeypatch.setattr(ingestion, "market_session_state", lambda: {"state": "CLOSED"})
    # The first zero-data stock is reinserted until 5 failures; start at 4 so
    # the next cycle must discover the newly restored stock.
    exclude(db, "9002", attempts=4)
    result = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, provider, job, stock_boundary_callback=boundary))
    assert calls == ["9002", "9001"] and result["status"] == "SUCCESS"
    assert stale_issue.no_data_attempts == 1
    assert db.get(StockRefreshIssue, "9001", populate_existing=True).no_data_attempts == 1


def test_unclassified_provider_interruption_keeps_exclusion(db, monkeypatch):
    exclude(db)
    job = manual.queue_manual_stock_refresh(db, "9001", END, recover_exclusion=True)
    async def fetch(*args, **kwargs):
        return {"status": "DATA_INSUFFICIENT", "datasets": {"source": {"retryable_pending": 1}}}
    monkeypatch.setattr(manual, "fetch_and_score_stock", fetch)
    result = asyncio.run(manual.resume_manual_stock_refresh(db, client(), job))
    assert result["status"] == "WAITING_FOR_PROVIDER" and "9001" in ingestion.skipped_refresh_stock_ids(db)


def test_source_empty_checkpoint_is_retried_without_force_refresh(tmp_path, monkeypatch):
    provider = FinMindClient(Settings(raw_root=tmp_path, source_concurrency=1))
    calls = []
    def fetch(dataset, sid, start, end):
        calls.append((start, end))
        return [], {"empty_is_valid": True, "empty_reason": "no_provider_observation", "empty_observation_dates": ["2026-08-26", "2026-08-27"], "attempt": 1}
    monkeypatch.setattr(provider, "fetch", fetch)
    asyncio.run(provider.fetch_stocks_dataset(["9001"], "TaiwanStockPrice", "2026-08-26", "2026-08-27"))
    calls.clear()
    result = asyncio.run(provider.fetch_stocks_dataset(["9001"], "TaiwanStockPrice", "2026-08-26", "2026-08-27", retry_provider_missing=True))
    assert calls and result["physical_requests"] == 1


def test_queue_failure_rolls_back_new_job_and_leaves_issue(db, monkeypatch):
    exclude(db)
    monkeypatch.setattr(manual, "snapshot_issue", lambda issue: (_ for _ in ()).throw(RuntimeError("snapshot failure")))
    with pytest.raises(RuntimeError):
        manual.queue_manual_stock_refresh(db, "9001", END, recover_exclusion=True)
    assert db.query(JobRun).count() == db.query(RefreshExclusionRecovery).count() == 0
    assert "9001" in ingestion.skipped_refresh_stock_ids(db)
