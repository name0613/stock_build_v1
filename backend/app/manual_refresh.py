"""Durable manual stock requests, executed only by the worker provider lane."""
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import JobRun, RefreshExclusionRecovery, StockRefreshIssue
from .finmind import FinMindClient, FinMindError, GLOBAL_PROVIDER_FAILURE_CODES
from .ingestion import fetch_and_score_stock, _job_finish
from .refresh_exclusions import manual_transaction, recovery_payload, snapshot_issue
from .refresh_policy import REFRESH_NO_DATA_LIMIT

MANUAL_STOCK_REFRESH_DATASET = "manual_stock_refresh_score"
MANUAL_ACTIVE_STATUSES = ("QUEUED", "RUNNING", "WAITING_FOR_QUOTA", "WAITING_FOR_PROVIDER")

def manual_stock_job_payload(job: JobRun, db: Session | None = None) -> dict[str, Any]:
    checkpoint = job.checkpoint_state if isinstance(job.checkpoint_state, dict) else {}
    return {
        "recovery": recovery_payload(db.get(RefreshExclusionRecovery, job.id, populate_existing=True)) if db is not None else None,
        "target_readiness": checkpoint.get("target_readiness"),
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


def queue_manual_stock_refresh(db: Session, stock_id: str, target: date, *, recover_exclusion: bool = False) -> JobRun:
    with manual_transaction(db):
        job = db.scalar(select(JobRun).where(
            JobRun.dataset == MANUAL_STOCK_REFRESH_DATASET,
            JobRun.status.in_(MANUAL_ACTIVE_STATUSES),
            JobRun.checkpoint_state["stock_id"].as_string() == stock_id,
        ).order_by(JobRun.id).limit(1).execution_options(populate_existing=True))
        issue = db.get(StockRefreshIssue, stock_id, populate_existing=True) if recover_exclusion else None
        if recover_exclusion and (issue is None or issue.no_data_attempts < REFRESH_NO_DATA_LIMIT):
            # A retry after completion returns its durable receipt without another download.
            previous = db.scalar(select(JobRun).join(RefreshExclusionRecovery).where(
                RefreshExclusionRecovery.stock_id == stock_id).order_by(JobRun.id.desc()).limit(1))
            if previous is not None:
                return previous
            raise ValueError("STOCK_NOT_EXCLUDED")
        if job is None:
            job = JobRun(
                dataset=MANUAL_STOCK_REFRESH_DATASET, status="QUEUED",
                requested_date=target, requested_start_date=target, requested_end_date=target,
                started_at=datetime.now(timezone.utc), stocks_attempted=1,
                checkpoint_state={"stock_id": stock_id, "target_date": target.isoformat(), "run_mode": "targeted_fetch_and_score", "phase": "queued", "datasets": {}, "progress": {"completed": 0, "total": 5}},
            )
            db.add(job)
            db.flush()
        if recover_exclusion and db.get(RefreshExclusionRecovery, job.id) is None:
            db.add(RefreshExclusionRecovery(job_id=job.id, stock_id=stock_id,
                requested_at=datetime.now(timezone.utc), previous_issue=snapshot_issue(issue), result={}))
    return job


def manual_job_due(job: JobRun) -> bool:
    retry = (job.checkpoint_state or {}).get("next_retry_at")
    if not retry:
        return True
    parsed = datetime.fromisoformat(str(retry).replace("Z", "+00:00"))
    return parsed <= datetime.now(timezone.utc)


async def resume_manual_stock_refresh(db: Session, client: FinMindClient, job: JobRun, *, progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
    db.refresh(job)
    if job.status not in MANUAL_ACTIVE_STATUSES or not manual_job_due(job):
        return manual_stock_job_payload(job, db)
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
        return manual_stock_job_payload(job, db)

    try:
        quota = client.provider_quota(source_revision=client.settings.source_revision)
        if int(quota.get("provider_reported_remaining") or 0) <= max(0, client.settings.broker_quota_reserve):
            return wait("WAITING_FOR_QUOTA", "QUOTA_EXHAUSTED")
        refreshed = {name for name, value in previous.get("datasets", {}).items() if isinstance(value, dict) and value.get("refresh_complete") is True}
        result = await fetch_and_score_stock(db, client, stock_id, job.requested_end_date, job=job, progress_callback=progress_callback, refreshed_datasets=refreshed, defer_finish=True, reuse_broker_observations=True)
        job.checkpoint_state = {**job.checkpoint_state, "worker_started_at": worker_started_at}
        db.commit()
        codes = {str(item.get("error_code")) for item in result.get("fetch_errors", [])}
        codes.update(str(code) for value in result.get("datasets", {}).values() if isinstance(value, dict)
                     for code in value.get("failure_codes", []))
        if "QUOTA_EXHAUSTED" in codes or any(int(value.get("quota_unselected_pending_count", 0) or 0) > 0 for value in result.get("datasets", {}).values() if isinstance(value, dict)):
            return wait("WAITING_FOR_QUOTA", "QUOTA_EXHAUSTED")
        terminal_codes = codes & (GLOBAL_PROVIDER_FAILURE_CODES | {"NON_RETRYABLE_4XX", "STOCK_SCHEMA_MISMATCH"})
        retry_codes = codes - {"EMPTY_RESPONSE_UNVERIFIED", "PARTIAL_OBSERVATION_COVERAGE"}
        unclassified_pending = any(
            int(value.get("retryable_pending", 0) or 0) > 0 and not value.get("failure_codes")
            for value in result.get("datasets", {}).values() if isinstance(value, dict)
        )
        if terminal_codes:
            _job_finish(db, job, "FAILED", error_code=sorted(terminal_codes)[0], checkpoint_state={**job.checkpoint_state, "phase": "failed"})
        elif retry_codes:
            return wait("WAITING_FOR_PROVIDER", sorted(retry_codes)[0])
        elif unclassified_pending:
            return wait("WAITING_FOR_PROVIDER", "REFRESH_INCOMPLETE")
        else:
            # Only this wrapper decides whether a durable request is truly terminal.
            _job_finish(db, job, result.get("status", job.status if job.status not in MANUAL_ACTIVE_STATUSES else "DATA_INSUFFICIENT"),
                records=sum(int(value.get("records_accepted", 0) or 0) for value in result.get("datasets", {}).values()),
                stocks_completed=1 if result.get("status") == "SUCCESS" else 0,
                checkpoint_state={**job.checkpoint_state, "phase": "completed"})
        return manual_stock_job_payload(job, db)
    except FinMindError as exc:
        if exc.code not in {"AUTHENTICATION_FAILED", "ACCESS_DENIED", "SCHEMA_MISMATCH"}:
            return wait("WAITING_FOR_QUOTA" if exc.code == "QUOTA_EXHAUSTED" else "WAITING_FOR_PROVIDER", exc.code)
        raise


