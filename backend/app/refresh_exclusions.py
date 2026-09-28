"""Local-only exclusion management and atomic, exactly-once release."""
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import RLock

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from .models import JobRun, RefreshExclusionRecovery, Stock, StockRefreshIssue
from .refresh_policy import REFRESH_NO_DATA_LIMIT, exclusion_predicate

MANUAL_DATASET = "manual_stock_refresh_score"
ACTIVE_STATUSES = ("QUEUED", "RUNNING", "WAITING_FOR_QUOTA", "WAITING_FOR_PROVIDER")
_manual_lock = RLock()


@contextmanager
def manual_transaction(db: Session):
    """Serialize queue/intent merging and finalization across API/worker processes."""
    with _manual_lock:
        try:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(8202609083501)"))
            elif db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            yield
            db.commit()
        except BaseException:
            db.rollback()
            raise


def recovery_payload(recovery: RefreshExclusionRecovery | None) -> dict | None:
    if recovery is None:
        return None
    return {"requested_at": recovery.requested_at, "released_at": recovery.released_at,
            "automatic_refresh_eligibility": "RESTORED" if recovery.released_at else "EXCLUDED_UNTIL_FINISHED",
            "previous_issue": recovery.previous_issue, "result": recovery.result}


def snapshot_issue(issue: StockRefreshIssue) -> dict:
    return {"no_data_attempts": issue.no_data_attempts, "status": issue.status,
            "reason_code": issue.reason_code, "details": issue.details,
            "first_attempt_at": issue.first_attempt_at.isoformat() if issue.first_attempt_at else None,
            "last_attempt_at": issue.last_attempt_at.isoformat() if issue.last_attempt_at else None,
            "last_job_id": issue.last_job_id}


def release_exclusion(db: Session, job: JobRun) -> None:
    """Called inside the final job transaction; never commit separately."""
    recovery = db.scalar(select(RefreshExclusionRecovery).where(
        RefreshExclusionRecovery.job_id == job.id,
    ).execution_options(populate_existing=True).with_for_update())
    if recovery is None or recovery.released_at is not None or job.status in ACTIVE_STATUSES:
        return
    issue = db.get(StockRefreshIssue, recovery.stock_id, populate_existing=True)
    if issue is not None:
        issue.no_data_attempts = 0
        issue.status = "RECOVERED"
        issue.reason_code = "MANUAL_EXCLUSION_RELEASED"
        issue.details = {}
    recovery.released_at = job.finished_at or datetime.now(timezone.utc)
    state = job.checkpoint_state or {}
    recovery.result = {"status": job.status, "error_code": job.error_code,
                       **{key: state.get(key) for key in ("score", "readiness", "target_readiness", "target_date", "evaluated_source_date", "fallback_applied", "fetch_errors")}}


def list_exclusions(db: Session, *, search: str = "", market: str = "", page: int = 1, page_size: int = 50) -> dict:
    from .manual_refresh import manual_stock_job_payload
    query = select(Stock, StockRefreshIssue).join(StockRefreshIssue).where(exclusion_predicate())
    total = db.scalar(select(func.count()).select_from(StockRefreshIssue).where(exclusion_predicate())) or 0
    if search.strip():
        query = query.where(or_(Stock.stock_id.contains(search.strip(), autoescape=True), Stock.stock_name.contains(search.strip(), autoescape=True)))
    if market:
        query = query.where(Stock.market == market)
    filtered = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(query.order_by(Stock.stock_id).offset((page - 1) * page_size).limit(page_size)).all()
    ids = [stock.stock_id for stock, _ in rows]
    jobs = db.scalars(select(JobRun).where(JobRun.dataset == MANUAL_DATASET,
        JobRun.status.in_(ACTIVE_STATUSES), JobRun.checkpoint_state["stock_id"].as_string().in_(ids)).order_by(JobRun.id)).all()
    active = {(job.checkpoint_state or {}).get("stock_id"): manual_stock_job_payload(job, db) for job in jobs}
    items = []
    for stock, issue in rows:
        reason = "INCOMPLETE" if issue.reason_code.startswith("INCOMPLETE") else "NO_DATA" if issue.reason_code.startswith("NO_DATA") else None
        items.append({"stock_id": stock.stock_id, "stock_name": stock.stock_name, "market": stock.market,
                      "no_data_attempts": issue.no_data_attempts, "attempt_limit": REFRESH_NO_DATA_LIMIT,
                      "reason": reason, "reason_code": issue.reason_code,
                      "missing_sources": (issue.details or {}).get("incomplete_datasets"),
                      "last_attempt_at": issue.last_attempt_at, "job": active.get(stock.stock_id)})
    # Active requests survive reload even if the user moves to another results page.
    active_jobs = db.scalars(select(JobRun).join(RefreshExclusionRecovery).where(JobRun.status.in_(ACTIVE_STATUSES)).order_by(JobRun.id)).all()
    recent_jobs = db.scalars(select(JobRun).join(RefreshExclusionRecovery).where(RefreshExclusionRecovery.released_at.is_not(None)).order_by(RefreshExclusionRecovery.released_at.desc()).limit(20)).all()
    return {"total": total, "filtered_total": filtered, "page": page, "page_size": page_size, "items": items,
            "active_jobs": [manual_stock_job_payload(job, db) for job in active_jobs],
            "recent_results": [manual_stock_job_payload(job, db) for job in recent_jobs]}
