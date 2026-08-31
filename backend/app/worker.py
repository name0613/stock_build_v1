from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from zoneinfo import ZoneInfo
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import get_settings
from .db import SessionLocal, init_db
from .finmind import FinMindClient, FinMindRequestBudget
from .ingestion import FAVORITE_REFRESH_DATASET, UNIVERSE_BUDGET_LIMIT, UNIVERSE_BUDGET_REFRESH_DATASET, catch_up, intraday_sync, resume_favorite_refresh_job, resume_universe_budget_refresh_job, seed_score_version
from .calendar import MARKET_CLOSE_TIME, MARKET_OPEN_TIME, completed_source_end_date, is_trading_session, market_session_state, source_publication_window_open
from .models import JobRun
from .worker_health import start_health_server

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()
OPEN_MARKET_SYNC_JOB_ID = "market-open-sync"
SCHEDULE_CONTRACT = {"main-sync": (21, 30), "retry-sync": (23, 0), OPEN_MARKET_SYNC_JOB_ID: (9, 0)}
_heartbeat_lock = Lock()
_scheduler_state_lock = Lock()
_provider_work_lock = Lock()
_scheduler_runtime: BlockingScheduler | None = None
_scheduler_job_state: dict[str, dict[str, Any]] = {}


def _heartbeat(**updates: object) -> None:
    with _heartbeat_lock:
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
    return completed_source_end_date(datetime.now(ZoneInfo(settings.timezone)))


def _startup_catch_up_allowed(now: datetime | None = None) -> bool:
    """Do not launch provider work before the nightly source window."""
    return source_publication_window_open(now or datetime.now(ZoneInfo(settings.timezone)))


def _next_open_market_fire_at(now: datetime | None = None) -> str:
    """Return the next 30-minute fire inside a valid Taiwan trading session."""
    current = now or datetime.now(timezone.utc)
    market_tz = ZoneInfo(settings.timezone)
    local = current.astimezone(market_tz)
    for day_offset in range(0, 8):
        day = (local + timedelta(days=day_offset)).date()
        if not is_trading_session(day):
            continue
        candidate = datetime.combine(day, MARKET_OPEN_TIME, tzinfo=market_tz)
        close_at = datetime.combine(day, MARKET_CLOSE_TIME, tzinfo=market_tz)
        while candidate < close_at:
            if candidate > local:
                return candidate.astimezone(timezone.utc).isoformat()
            candidate += timedelta(minutes=30)
    raise RuntimeError("unable to calculate next open-market scheduled fire")


def _next_scheduled_run_at(now: datetime | None = None) -> str:
    """Return the next scheduled run for the worker health contract."""
    current = now or datetime.now(timezone.utc)
    candidates = [datetime.fromisoformat(_next_job_fire_at(job_id, current)) for job_id in SCHEDULE_CONTRACT]
    return min(candidates).astimezone(timezone.utc).isoformat()


def _next_job_fire_at(job_id: str, now: datetime | None = None) -> str:
    """Return the next fire for one registered job in the canonical schedule."""
    if job_id not in SCHEDULE_CONTRACT:
        raise ValueError(f"unknown scheduled job: {job_id}")
    if job_id == OPEN_MARKET_SYNC_JOB_ID:
        return _next_open_market_fire_at(now)
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(ZoneInfo(settings.timezone))
    hour, minute = SCHEDULE_CONTRACT[job_id]
    for day_offset in range(0, 8):
        day = local + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > local:
            return candidate.astimezone(timezone.utc).isoformat()
    raise RuntimeError("unable to calculate next scheduled fire")


def _initial_scheduler_job_state(now: datetime | None = None) -> dict[str, dict[str, Any]]:
    return {
        job_id: {
            "next_expected_fire_at": _next_job_fire_at(job_id, now),
            "last_started_fire_at": None,
            "last_completed_fire_at": None,
            "last_event": "REGISTERED",
            "last_event_at": (now or datetime.now(timezone.utc)).isoformat(),
            "last_error_code": None,
        }
        for job_id in SCHEDULE_CONTRACT
    }


def _scheduler_listener(event: Any) -> None:
    """Persist APScheduler execution events; pulse liveness cannot forge them."""
    job_id = str(getattr(event, "job_id", ""))
    if job_id not in SCHEDULE_CONTRACT:
        return
    now = datetime.now(timezone.utc)
    scheduled_times = list(getattr(event, "scheduled_run_times", []) or [])
    scheduled = getattr(event, "scheduled_run_time", None) or (scheduled_times[-1] if scheduled_times else None)
    scheduled = scheduled if isinstance(scheduled, datetime) else now
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    with _scheduler_state_lock:
        state = _scheduler_job_state.setdefault(job_id, _initial_scheduler_job_state(now)[job_id])
        if event.code == EVENT_JOB_SUBMITTED:
            state.update(
                last_started_fire_at=scheduled.isoformat(),
                next_expected_fire_at=_next_job_fire_at(job_id, scheduled),
                last_event="STARTED",
                last_event_at=now.isoformat(),
                last_error_code=None,
            )
        elif event.code == EVENT_JOB_EXECUTED:
            state.update(last_completed_fire_at=scheduled.isoformat(), last_event="COMPLETED", last_event_at=now.isoformat(), last_error_code=None)
        elif event.code == EVENT_JOB_MISSED:
            state.update(last_missed_fire_at=scheduled.isoformat(), next_expected_fire_at=_next_job_fire_at(job_id, scheduled), last_event="MISSED", last_event_at=now.isoformat(), last_error_code="SCHEDULED_FIRE_MISSED")
        elif event.code == EVENT_JOB_ERROR:
            state.update(last_error_fire_at=scheduled.isoformat(), last_event="ERROR", last_event_at=now.isoformat(), last_error_code="SCHEDULED_FIRE_ERROR")
        snapshot = json.loads(json.dumps(_scheduler_job_state))
    _heartbeat(
        registered_scheduler_job_ids=sorted(SCHEDULE_CONTRACT),
        scheduler_jobs=snapshot,
        last_scheduler_event_at=now.isoformat(),
    )


def _reconcile_interrupted_jobs(db: object) -> None:
    for job in db.query(JobRun).filter(JobRun.status == "RUNNING").all():
        if job.dataset in {FAVORITE_REFRESH_DATASET, UNIVERSE_BUDGET_REFRESH_DATASET}:
            checkpoint = dict(job.checkpoint_state or {})
            checkpoint["phase"] = "queued_after_worker_restart"
            checkpoint["next_retry_at"] = None
            job.status = "QUEUED"
            job.error_code = "WORKER_RESTARTED_RESUMING"
            job.error = None
            job.finished_at = None
            job.checkpoint_state = checkpoint
            continue
        job.status = "PARTIAL"
        job.error_code = "WORKER_RESTARTED"
        job.error = "worker restarted before job completion"
        job.finished_at = datetime.now(timezone.utc)
    db.commit()


def _heartbeat_pulse() -> None:
    while True:
        time.sleep(30)
        try:
            scheduler_ready = bool(_scheduler_runtime is not None and _scheduler_runtime.running)
            updates: dict[str, object] = {"scheduler_ready": scheduler_ready, "market_session": market_session_state()}
            if scheduler_ready:
                updates["last_scheduler_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
            _heartbeat(**updates)
        except OSError:
            logger.warning("worker heartbeat write failed")


def run_catch_up() -> None:
    if not _provider_work_lock.acquire(blocking=False):
        _heartbeat(status="idle", ready=True, last_job_status="DEFERRED_PROVIDER_WORK_BUSY", last_error_code=None)
        return
    started = datetime.now(timezone.utc).isoformat()
    _heartbeat(status="running", ready=True, scheduler_ready=False, market_session=market_session_state(), last_scheduler_heartbeat_at=started, last_job_started_at=started, last_error_code=None)
    db = SessionLocal()
    try:
        def report_progress(phase: str) -> None:
            running = db.query(JobRun).filter(JobRun.status == "RUNNING").order_by(JobRun.id.desc()).first()
            _heartbeat(last_job_progress_at=datetime.now(timezone.utc).isoformat(), job_phase=phase, current_job_run_id=running.id if running else None)

        result = asyncio.run(catch_up(db, FinMindClient(settings), end_date=_completed_source_end_date(), progress_callback=report_progress))
        logger.info("catch-up completed status=%s datasets=%s", result.get("status"), result.get("datasets"))
        finished = datetime.now(timezone.utc).isoformat()
        _heartbeat(status="idle", ready=True, scheduler_ready=bool(_scheduler_runtime and _scheduler_runtime.running), market_session=market_session_state(), last_job_finished_at=finished, last_job_status=result.get("status"), last_error_code=result.get("fatal_code"), current_job_run_id=None)
    except Exception as exc:
        logger.error("catch-up failed code=%s", getattr(exc, "code", "UNEXPECTED"))
        finished = datetime.now(timezone.utc).isoformat()
        _heartbeat(status="idle", ready=True, scheduler_ready=bool(_scheduler_runtime and _scheduler_runtime.running), market_session=market_session_state(), last_job_finished_at=finished, last_job_status="FAILED", last_error_code=getattr(exc, "code", "UNEXPECTED"), current_job_run_id=None)
    finally:
        db.close()
        _provider_work_lock.release()


def run_open_market_sync() -> None:
    """Run only current-session source refreshes while the Taiwan market is open."""
    session = market_session_state()
    if session.get("state") != "OPEN":
        _heartbeat(
            status="idle",
            ready=True,
            scheduler_ready=bool(_scheduler_runtime and _scheduler_runtime.running),
            market_session=session,
            last_job_status="SKIPPED_MARKET_CLOSED",
            last_error_code=None,
            current_job_run_id=None,
        )
        return
    run_intraday_sync()


def run_intraday_sync() -> None:
    if not _provider_work_lock.acquire(blocking=False):
        _heartbeat(status="idle", ready=True, last_job_status="DEFERRED_PROVIDER_WORK_BUSY", last_error_code=None)
        return
    started = datetime.now(timezone.utc).isoformat()
    _heartbeat(status="running", ready=True, scheduler_ready=False, market_session=market_session_state(), last_scheduler_heartbeat_at=started, last_job_started_at=started, last_error_code=None)
    db = SessionLocal()
    try:
        def report_progress(phase: str) -> None:
            running = db.query(JobRun).filter(JobRun.status == "RUNNING").order_by(JobRun.id.desc()).first()
            _heartbeat(last_job_progress_at=datetime.now(timezone.utc).isoformat(), job_phase=phase, current_job_run_id=running.id if running else None)

        result = asyncio.run(intraday_sync(db, FinMindClient(settings), end_date=datetime.now(ZoneInfo(settings.timezone)).date(), progress_callback=report_progress))
        logger.info("intraday sync completed status=%s datasets=%s", result.get("status"), result.get("datasets"))
        finished = datetime.now(timezone.utc).isoformat()
        _heartbeat(status="idle", ready=True, scheduler_ready=bool(_scheduler_runtime and _scheduler_runtime.running), market_session=market_session_state(), last_job_finished_at=finished, last_job_status=result.get("status"), last_error_code=result.get("fatal_code"), current_job_run_id=None)
    except Exception as exc:
        logger.error("intraday sync failed code=%s", getattr(exc, "code", "UNEXPECTED"))
        finished = datetime.now(timezone.utc).isoformat()
        _heartbeat(status="idle", ready=True, scheduler_ready=bool(_scheduler_runtime and _scheduler_runtime.running), market_session=market_session_state(), last_job_finished_at=finished, last_job_status="FAILED", last_error_code=getattr(exc, "code", "UNEXPECTED"), current_job_run_id=None)
    finally:
        db.close()
        _provider_work_lock.release()


REFRESH_ACTIVE_STATUSES = ("QUEUED", "RUNNING", "WAITING_FOR_QUOTA", "WAITING_FOR_PROVIDER")


def _next_durable_refresh_job(db: object, dataset: str | None = None) -> JobRun | None:
    datasets = (dataset,) if dataset else (FAVORITE_REFRESH_DATASET, UNIVERSE_BUDGET_REFRESH_DATASET)
    return (
        db.query(JobRun)
        .filter(JobRun.dataset.in_(datasets), JobRun.status.in_(REFRESH_ACTIVE_STATUSES))
        .order_by(JobRun.id.asc())
        .first()
    )


def _run_durable_refresh(dataset: str | None = None) -> None:
    """Claim one provider lock, then dispatch the oldest durable refresh."""
    if not _provider_work_lock.acquire(blocking=False):
        return
    db = SessionLocal()
    try:
        job = _next_durable_refresh_job(db, dataset)
        if job is None:
            return

        def report_progress(phase: str) -> None:
            _heartbeat(last_job_progress_at=datetime.now(timezone.utc).isoformat(), job_phase=phase, current_job_run_id=job.id)

        if job.dataset == FAVORITE_REFRESH_DATASET:
            phase = "favorite_refresh"
            client = FinMindClient(settings)
            coroutine = resume_favorite_refresh_job(db, client, job, progress_callback=report_progress)
        else:
            phase = "universe_budget_refresh"
            checkpoint = dict(job.checkpoint_state or {})
            budget_state = checkpoint.get("budget") if isinstance(checkpoint.get("budget"), dict) else {}
            budget_file = settings.raw_root / "checkpoints" / f"universe-budget-job-{job.id}.json"
            request_budget = FinMindRequestBudget(
                UNIVERSE_BUDGET_LIMIT,
                budget_file,
                used=int(budget_state.get("used", 0) or 0),
            )
            client = FinMindClient(settings, request_budget=request_budget)
            coroutine = resume_universe_budget_refresh_job(db, client, job, progress_callback=report_progress)

        _heartbeat(status="running", ready=True, last_job_started_at=datetime.now(timezone.utc).isoformat(), current_job_run_id=job.id, job_phase=phase, last_error_code=None)
        result = asyncio.run(coroutine)
        _heartbeat(
            status="idle",
            ready=True,
            last_job_finished_at=datetime.now(timezone.utc).isoformat(),
            last_job_status=result.get("status"),
            last_error_code=result.get("error_code"),
            current_job_run_id=None,
            job_phase=result.get("phase"),
        )
    except Exception as exc:
        db.rollback()
        logger.error("durable refresh failed code=%s", getattr(exc, "code", "UNEXPECTED"))
        _heartbeat(status="idle", ready=True, last_job_status="FAILED", last_error_code=getattr(exc, "code", "UNEXPECTED"), current_job_run_id=None)
    finally:
        db.close()
        _provider_work_lock.release()


def run_durable_refresh() -> None:
    """Resume the oldest favorite or fixed-budget job without starvation."""
    _run_durable_refresh()


def run_favorite_refresh() -> None:
    """Compatibility entry point for a favorite-only manual invocation."""
    _run_durable_refresh(FAVORITE_REFRESH_DATASET)


def run_universe_budget_refresh() -> None:
    """Compatibility entry point for a universe-only manual invocation."""
    _run_durable_refresh(UNIVERSE_BUDGET_REFRESH_DATASET)


def main() -> None:
    global _scheduler_runtime, _scheduler_job_state
    _heartbeat(status="starting", ready=False, scheduler_ready=False, market_session=market_session_state(), scheduler_started_at=None)
    start_health_server(Path(settings.worker_heartbeat_file))
    Thread(target=_heartbeat_pulse, daemon=True, name="worker-heartbeat-pulse").start()
    init_db()
    db = SessionLocal()
    _reconcile_interrupted_jobs(db)
    seed_score_version(db)
    db.close()
    if _startup_catch_up_allowed():
        run_catch_up()
    else:
        _heartbeat(
            status="idle",
            ready=True,
            scheduler_ready=False,
            market_session=market_session_state(),
            last_job_status="DEFERRED_BEFORE_SOURCE_PUBLICATION",
            last_error_code=None,
            last_job_started_at=None,
            last_job_progress_at=None,
            job_phase=None,
            last_job_finished_at=datetime.now(timezone.utc).isoformat(),
            current_job_run_id=None,
        )
    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(run_catch_up, CronTrigger(day_of_week="mon-fri", hour=21, minute=30, timezone=settings.timezone), id="main-sync", replace_existing=True, misfire_grace_time=300)
    scheduler.add_job(run_catch_up, CronTrigger(day_of_week="mon-fri", hour=23, minute=0, timezone=settings.timezone), id="retry-sync", replace_existing=True, misfire_grace_time=300)
    scheduler.add_job(
        run_open_market_sync,
        OrTrigger(
            [
                CronTrigger(day_of_week="mon-fri", hour="9-12", minute="0,30", timezone=settings.timezone),
                CronTrigger(day_of_week="mon-fri", hour=13, minute=0, timezone=settings.timezone),
            ]
        ),
        id=OPEN_MARKET_SYNC_JOB_ID,
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(run_durable_refresh, IntervalTrigger(minutes=1, timezone=settings.timezone), id="durable-refresh-resume", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=60)
    scheduler.add_listener(_scheduler_listener, EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    _scheduler_runtime = scheduler
    logger.info("worker scheduled timezone=%s", settings.timezone)
    scheduler_started = datetime.now(timezone.utc).isoformat()
    with _scheduler_state_lock:
        _scheduler_job_state = _initial_scheduler_job_state()
        state_snapshot = json.loads(json.dumps(_scheduler_job_state))
    _heartbeat(status="idle", ready=True, scheduler_ready=True, market_session=market_session_state(), scheduler_started_at=scheduler_started, last_scheduler_heartbeat_at=scheduler_started, next_expected_run_at=_next_scheduled_run_at(), registered_scheduler_job_ids=sorted(SCHEDULE_CONTRACT), scheduler_jobs=state_snapshot, last_scheduler_event_at=scheduler_started)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
