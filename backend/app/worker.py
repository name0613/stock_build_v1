from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from zoneinfo import ZoneInfo
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_settings
from .db import SessionLocal, init_db
from .finmind import FinMindClient
from .ingestion import catch_up, seed_score_version
from .calendar import expected_trading_sessions
from .models import JobRun
from .worker_health import start_health_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


def _heartbeat(**updates: object) -> None:
    path = Path(settings.worker_heartbeat_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, object] = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
    current.update(updates)
    current["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _completed_source_end_date() -> object:
    now = datetime.now(ZoneInfo(settings.timezone))
    candidate = now.date() if now.hour >= 21 else now.date() - timedelta(days=1)
    return expected_trading_sessions(candidate, 1)[-1]


def _next_scheduled_run_at(now: datetime | None = None) -> str:
    """Return the next scheduled run for the worker health contract."""
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(ZoneInfo(settings.timezone))
    candidates = []
    for day_offset in range(0, 8):
        day = local + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        for hour, minute in ((21, 30), (23, 0)):
            candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > local:
                candidates.append(candidate)
    candidate = min(candidates)
    return candidate.astimezone(timezone.utc).isoformat()


def _reconcile_interrupted_jobs(db: object) -> None:
    for job in db.query(JobRun).filter(JobRun.status == "RUNNING").all():
        job.status = "PARTIAL"
        job.error_code = "WORKER_RESTARTED"
        job.error = "worker restarted before job completion"
        job.finished_at = datetime.now(timezone.utc)
    db.commit()


def _heartbeat_pulse() -> None:
    while True:
        time.sleep(30)
        try:
            path = Path(settings.worker_heartbeat_file)
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if payload.get("scheduler_ready"):
                _heartbeat(last_scheduler_heartbeat_at=datetime.now(timezone.utc).isoformat(), next_expected_run_at=_next_scheduled_run_at())
            else:
                _heartbeat()
        except OSError:
            logger.warning("worker heartbeat write failed")


def run_catch_up() -> None:
    started = datetime.now(timezone.utc).isoformat()
    _heartbeat(status="running", ready=True, scheduler_ready=False, last_scheduler_heartbeat_at=started, last_job_started_at=started, last_error_code=None)
    db = SessionLocal()
    try:
        result = asyncio.run(catch_up(db, FinMindClient(settings), end_date=_completed_source_end_date(), progress_callback=lambda phase: _heartbeat(last_job_progress_at=datetime.now(timezone.utc).isoformat(), job_phase=phase)))
        logger.info("catch-up completed status=%s datasets=%s", result.get("status"), result.get("datasets"))
        finished = datetime.now(timezone.utc).isoformat()
        _heartbeat(status="idle", ready=True, scheduler_ready=True, last_scheduler_heartbeat_at=finished, last_job_finished_at=finished, last_job_status=result.get("status"), last_error_code=result.get("fatal_code"))
    except Exception as exc:
        logger.error("catch-up failed code=%s", getattr(exc, "code", "UNEXPECTED"))
        finished = datetime.now(timezone.utc).isoformat()
        _heartbeat(status="idle", ready=True, scheduler_ready=True, last_scheduler_heartbeat_at=finished, last_job_finished_at=finished, last_job_status="FAILED", last_error_code=getattr(exc, "code", "UNEXPECTED"))
    finally:
        db.close()


def main() -> None:
    _heartbeat(status="starting", ready=False, scheduler_ready=False, scheduler_started_at=None)
    start_health_server(Path(settings.worker_heartbeat_file))
    Thread(target=_heartbeat_pulse, daemon=True, name="worker-heartbeat-pulse").start()
    init_db()
    db = SessionLocal()
    _reconcile_interrupted_jobs(db)
    seed_score_version(db)
    db.close()
    run_catch_up()
    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(run_catch_up, CronTrigger(day_of_week="mon-fri", hour=21, minute=30, timezone=settings.timezone), id="main-sync", replace_existing=True)
    scheduler.add_job(run_catch_up, CronTrigger(day_of_week="mon-fri", hour=23, minute=0, timezone=settings.timezone), id="retry-sync", replace_existing=True)
    logger.info("worker scheduled timezone=%s", settings.timezone)
    scheduler_started = datetime.now(timezone.utc).isoformat()
    _heartbeat(status="idle", ready=True, scheduler_ready=True, scheduler_started_at=scheduler_started, last_scheduler_heartbeat_at=scheduler_started, next_expected_run_at=_next_scheduled_run_at())
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
