"""Explicit, audited refresh-counter reset. Stop API/worker before production use.

Run inside the new backend image, with its existing database/raw-volume mounts:
python /app/scripts/reset_refresh_issues.py --reset-id <unique-operation-id>
The same ID is safe to retry; it cannot erase subsequent failure observations.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from sqlalchemy import delete, select, text

from app.calendar import CALENDAR_VERSION, closed_market_target_date
from app.config import get_settings
from app.db import SessionLocal
from app.ingestion import UNIVERSE_BUDGET_REFRESH_DATASET
from app.models import JobRun, Stock, StockRefreshIssue
from app.refresh_policy import REFRESH_NO_DATA_LIMIT

RESET_DATASET = "refresh_exclusion_reset"
ACTIVE = ("QUEUED", "RUNNING", "WAITING_FOR_QUOTA", "WAITING_FOR_PROVIDER")


def reset(db, reset_id, backup_root, *, now=None):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", reset_id):
        raise ValueError("reset ID must be a short identifier")
    now = now or datetime.now(timezone.utc)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(8202609083500)"))
        db.execute(text("SELECT pg_advisory_xact_lock(8202609083501)"))
    previous = db.scalar(select(JobRun).where(JobRun.dataset == RESET_DATASET,
                         JobRun.checkpoint_state["reset_id"].as_string() == reset_id))
    if previous:
        return {**previous.checkpoint_state, "replayed": True}
    issues = list(db.scalars(select(StockRefreshIssue).order_by(StockRefreshIssue.stock_id)))
    jobs = list(db.scalars(select(JobRun).where(JobRun.dataset == UNIVERSE_BUDGET_REFRESH_DATASET, JobRun.status.in_(ACTIVE))))
    serialize = lambda row: {column.name: getattr(row, column.name) for column in row.__table__.columns}
    snapshot = {"reset_id": reset_id, "created_at": now.isoformat(), "issues": [serialize(row) for row in issues], "active_universe_jobs": [serialize(row) for row in jobs]}
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / f"{reset_id}-{uuid.uuid4().hex}.json"
    data = (json.dumps(snapshot, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
    # Flush the durable audit before the transaction can delete any counters.
    with backup.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(backup, 0o600)
    if os.name != "nt":
        descriptor = os.open(backup_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    ids = list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True)).order_by(Stock.stock_id)))
    target = closed_market_target_date(now)
    db.execute(delete(StockRefreshIssue))
    for job in jobs:
        old = dict(job.checkpoint_state or {})
        job.status = "QUEUED"
        job.error_code = None
        job.finished_at = None
        job.requested_date = job.requested_start_date = job.requested_end_date = target
        job.stocks_attempted, job.stocks_completed, job.stocks_failed = len(ids), 0, 0
        job.checkpoint_state = {**old, "reset_id": reset_id, "target_date": target.isoformat(), "phase": "queued", "stock_ids": ids, "cycle_stock_ids": ids,
                                "queue_index": 0, "cycle": 1, "stocks_completed": 0, "current_stock_id": None, "current_stock_progress": {},
                                "deferred_stock_ids": [], "skipped_no_data_count": 0, "next_retry_at": None, "daily_completion": None}
    receipt = {"reset_id": reset_id, "cleared_issue_count": len(issues), "previously_excluded_count": sum(row.no_data_attempts >= REFRESH_NO_DATA_LIMIT for row in issues),
               "excluded_count_after_reset": 0, "universe_count": len(ids), "requeued_job_ids": [job.id for job in jobs], "target_date": target.isoformat(),
               "backup_path": str(backup), "backup_sha256": hashlib.sha256(data).hexdigest(), "calendar_version": CALENDAR_VERSION,
               "source_revision": get_settings().source_revision, "completed_at": now.isoformat()}
    db.add(JobRun(dataset=RESET_DATASET, status="SUCCESS", started_at=now, finished_at=now, checkpoint_state=receipt))
    db.commit()
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset-id", required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        receipt = reset(db, args.reset_id, get_settings().raw_root / "operations" / "refresh-exclusion-backups")
    print(json.dumps(receipt, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
