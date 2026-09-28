"""Exercise recovery against PostgreSQL in a disposable schema, never public data.

Run inside the backend image with its normal database secret mount. No provider
calls are made. The random schema is removed even when a check fails.
"""
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
import json
import multiprocessing
from pathlib import Path
import re
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.ingestion import _job_finish, _record_no_data_attempt, skipped_refresh_stock_ids
from app.manual_refresh import queue_manual_stock_refresh
from app.models import Base, JobRun, RefreshExclusionRecovery, Stock, StockRefreshIssue
from app.refresh_completion import daily_refresh_completion
from app.refresh_exclusions import list_exclusions
from app.refresh_queue import queue_universe_budget_refresh

TARGET = date(2026, 9, 8)
NOW = datetime.now(timezone.utc)


def scoped_engine(schema):
    if not re.fullmatch(r"exclusion_probe_[0-9a-f]{16}", schema):
        raise ValueError("unexpected probe schema")
    return create_engine(get_settings().resolved_database_url(),
                         connect_args={"options": f"-csearch_path={schema}"})


def enqueue_in_process(schema):
    engine = scoped_engine(schema)
    try:
        with sessionmaker(engine, expire_on_commit=False, autoflush=False)() as db:
            return queue_manual_stock_refresh(db, "9001", TARGET, recover_exclusion=True).id
    finally:
        engine.dispose()


def run():
    root = create_engine(get_settings().resolved_database_url())
    if root.dialect.name != "postgresql":
        raise RuntimeError("this probe requires PostgreSQL")
    schema = f"exclusion_probe_{uuid.uuid4().hex[:16]}"
    engine = None
    checks = {}
    try:
        with root.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {schema}"))
        engine = scoped_engine(schema)
        # Start with the preceding schema, then execute the real migration twice.
        Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables
                                                if table.name != "refresh_exclusion_recoveries"])
        migration = Path("/app/migrations/014_refresh_exclusion_recoveries.sql").read_text(encoding="utf-8")
        with engine.begin() as connection:
            connection.execute(text(migration))
            connection.execute(text(migration))
        checks["migration_idempotent"] = True
        sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
        with sessions() as db:
            db.add(Stock(stock_id="9001", stock_name="Probe only", market="上市", is_common_stock=True))
            db.flush()
            db.add(StockRefreshIssue(stock_id="9001", no_data_attempts=5, status="LEGACY",
                reason_code="INCOMPLETE_AFTER_TWO_FETCHES", first_attempt_at=NOW, last_attempt_at=NOW,
                details={"incomplete_datasets": ["TaiwanStockPrice"]}))
            db.commit()
            first, _ = queue_universe_budget_refresh(db, TARGET, automatic=True, now=NOW)
            first_id, old_state = first.id, dict(first.checkpoint_state)
        # Different processes have different Python locks: this tests the DB lock.
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            ids = list(pool.map(enqueue_in_process, [schema] * 8))
        assert len(set(ids)) == 1
        checks["cross_process_deduplication"] = True
        with sessions() as db:
            listing = list_exclusions(db)
            assert listing["total"] == 1 and listing["items"][0]["reason"] == "INCOMPLETE"
            assert listing["items"][0]["job"]["recovery"]["released_at"] is None
            checks["postgres_json_query_and_intent"] = True
            job = db.get(JobRun, ids[0])
            job.status = "RUNNING"
            db.commit()
            import app.refresh_exclusions as exclusions
            original = exclusions.release_exclusion
            class SimulatedCrash(BaseException):
                pass
            def crash(session, job):
                original(session, job)
                session.flush()
                raise SimulatedCrash()
            exclusions.release_exclusion = crash
            try:
                _job_finish(db, job, "DATA_INSUFFICIENT", checkpoint_state={"phase": "completed"})
            except SimulatedCrash:
                pass
            finally:
                exclusions.release_exclusion = original
            db.expire_all()
            assert job.status == "RUNNING" and db.get(StockRefreshIssue, "9001").no_data_attempts == 5
            assert db.get(RefreshExclusionRecovery, job.id).released_at is None
            checks["completion_crash_atomic_rollback"] = True
            _job_finish(db, job, "DATA_INSUFFICIENT", checkpoint_state={"phase": "completed"})
            assert not skipped_refresh_stock_ids(db)
            assert db.get(RefreshExclusionRecovery, job.id).released_at is not None
            assert db.get(StockRefreshIssue, "9001").no_data_attempts == 0
            listing = list_exclusions(db)
            assert listing["total"] == 0 and listing["recent_results"][0]["job_id"] == job.id
            checks["atomic_release_and_receipt"] = True
            state = daily_refresh_completion(db, TARGET)
            assert state["pending_count"] == 1 and state["excluded_count"] == 0
            next_job, created = queue_universe_budget_refresh(db, TARGET, automatic=True, now=NOW)
            assert created and next_job.id != first_id and next_job.checkpoint_state["stock_ids"] == ["9001"]
            assert db.get(JobRun, first_id).checkpoint_state == old_state
            checks["completed_schedule_reopens_without_rewriting_history"] = True
            _record_no_data_attempt(db, "9001", next_job.id, {})
            db.commit()
            _job_finish(db, job, "FAILED", checkpoint_state={"phase": "failed"})
            assert job.status == "DATA_INSUFFICIENT" and job.checkpoint_state["phase"] == "completed"
            assert db.get(StockRefreshIssue, "9001").no_data_attempts == 1
            assert db.get(RefreshExclusionRecovery, job.id).result["status"] == "DATA_INSUFFICIENT"
            checks["replay_preserves_new_failure_count_and_audit"] = True
        return {"database": "postgresql", "checks": checks, "passed": all(checks.values()),
                "public_data_modified": False, "provider_requests": 0, "secrets_included": False}
    finally:
        if engine is not None:
            engine.dispose()
        with root.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        root.dispose()


if __name__ == "__main__":
    print(json.dumps(run()))
