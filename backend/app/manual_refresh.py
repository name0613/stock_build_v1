"""Durable manual stock requests, executed only by the worker provider lane."""
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from .models import JobRun
from .finmind import FinMindClient, FinMindError, GLOBAL_PROVIDER_FAILURE_CODES
from .ingestion import fetch_and_score_stock

MANUAL_STOCK_REFRESH_DATASET = "manual_stock_refresh_score"
MANUAL_ACTIVE_STATUSES = ("QUEUED", "RUNNING", "WAITING_FOR_QUOTA", "WAITING_FOR_PROVIDER")
_queue_lock = Lock()

def manual_stock_job_payload(job: JobRun) -> dict[str, Any]:
    checkpoint = job.checkpoint_state if isinstance(job.checkpoint_state, dict) else {}
    return {
        "job_id": job.id,
        "stock_id": checkpoint.get("stock_id"),
        "status": job.status,
        "run_mode": checkpoint.get("run_mode", "targeted_fetch_and_score"),
        "target_date": job.requested_end_date,
        "evaluated_source_date": checkpoint.get("evaluated_source_date"),
        "fallback_applied": bool(checkpoint.get("fallback_applied", False)),
        "fallback_reason": checkpoint.get("fallback_reason"),
        "phase": checkpoint.get("phase"),
        "progress": checkpoint.get("progress", {"completed": 0, "total": 5}),
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "datasets": checkpoint.get("datasets", {}),
        "pre_readiness": checkpoint.get("pre_readiness"),
        "readiness": checkpoint.get("readiness"),
        "score": checkpoint.get("score"),
        "fetch_errors": checkpoint.get("fetch_errors", []),
        "quota": checkpoint.get("quota"),
        "error_code": job.error_code,
        "next_retry_at": checkpoint.get("next_retry_at"),
    }


def queue_manual_stock_refresh(db: Session, stock_id: str, target: date) -> JobRun:
    with _queue_lock:
        try:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(8202609083501)"))
            job = db.scalar(select(JobRun).where(
                JobRun.dataset == MANUAL_STOCK_REFRESH_DATASET,
                JobRun.status.in_(MANUAL_ACTIVE_STATUSES),
                JobRun.checkpoint_state["stock_id"].as_string() == stock_id,
            ).order_by(JobRun.id).limit(1))
            if job is None:
                job = JobRun(
                    dataset=MANUAL_STOCK_REFRESH_DATASET, status="QUEUED",
                    requested_date=target, requested_start_date=target, requested_end_date=target,
                    started_at=datetime.now(timezone.utc), stocks_attempted=1,
                    checkpoint_state={"stock_id": stock_id, "target_date": target.isoformat(), "run_mode": "targeted_fetch_and_score", "phase": "queued", "datasets": {}, "progress": {"completed": 0, "total": 5}},
                )
                db.add(job)
            db.commit()
            return job
        except Exception:
            db.rollback()
            raise


def manual_job_due(job: JobRun) -> bool:
    retry = (job.checkpoint_state or {}).get("next_retry_at")
    if not retry:
        return True
    parsed = datetime.fromisoformat(str(retry).replace("Z", "+00:00"))
    return parsed <= datetime.now(timezone.utc)


async def resume_manual_stock_refresh(db: Session, client: FinMindClient, job: JobRun, *, progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
    if job.status not in MANUAL_ACTIVE_STATUSES or not manual_job_due(job):
        return manual_stock_job_payload(job)
    previous = dict(job.checkpoint_state or {})
    stock_id = str(previous["stock_id"])
    job.status = "RUNNING"
    job.finished_at = None
    job.error_code = None
    job.error = None
    worker_started_at = datetime.now(timezone.utc).isoformat()
    job.checkpoint_state = {**previous, "phase": "quota_check", "next_retry_at": None, "worker_started_at": worker_started_at}
    db.commit()
    if progress_callback:
        progress_callback(f"manual_stock_refresh:{stock_id}")

    def wait(status: str, code: str) -> dict[str, Any]:
        job.status = status
        job.finished_at = None
        job.error_code = code
        job.checkpoint_state = {**job.checkpoint_state, "phase": "waiting_for_quota" if status == "WAITING_FOR_QUOTA" else "waiting_for_provider", "next_retry_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
        db.commit()
        return manual_stock_job_payload(job)

    try:
        quota = client.provider_quota(source_revision=client.settings.source_revision)
        if int(quota.get("provider_reported_remaining") or 0) <= max(0, client.settings.broker_quota_reserve):
            return wait("WAITING_FOR_QUOTA", "QUOTA_EXHAUSTED")
        refreshed = {name for name, value in previous.get("datasets", {}).items() if isinstance(value, dict) and value.get("refresh_complete") is True}
        result = await fetch_and_score_stock(db, client, stock_id, job.requested_end_date, job=job, progress_callback=progress_callback, refreshed_datasets=refreshed)
        job.checkpoint_state = {**job.checkpoint_state, "worker_started_at": worker_started_at}
        db.commit()
        codes = {str(item.get("error_code")) for item in result.get("fetch_errors", [])}
        if "QUOTA_EXHAUSTED" in codes or any(int(value.get("quota_unselected_pending_count", 0) or 0) > 0 for value in result.get("datasets", {}).values() if isinstance(value, dict)):
            return wait("WAITING_FOR_QUOTA", "QUOTA_EXHAUSTED")
        if codes & GLOBAL_PROVIDER_FAILURE_CODES:
            job.status = "FAILED"
            job.error_code = sorted(codes & GLOBAL_PROVIDER_FAILURE_CODES)[0]
            db.commit()
        elif codes:
            return wait("WAITING_FOR_PROVIDER", sorted(codes)[0])
        return manual_stock_job_payload(job)
    except FinMindError as exc:
        if exc.code not in {"AUTHENTICATION_FAILED", "ACCESS_DENIED", "SCHEMA_MISMATCH"}:
            return wait("WAITING_FOR_QUOTA" if exc.code == "QUOTA_EXHAUSTED" else "WAITING_FOR_PROVIDER", exc.code)
        raise


