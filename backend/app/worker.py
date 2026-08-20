from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_settings
from .db import SessionLocal, init_db
from .finmind import FinMindClient
from .ingestion import catch_up, seed_score_version
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


def run_catch_up() -> None:
    started = datetime.now(timezone.utc).isoformat()
    _heartbeat(status="running", ready=True, last_job_started_at=started, last_error_code=None)
    db = SessionLocal()
    try:
        result = asyncio.run(catch_up(db, FinMindClient(settings)))
        logger.info("catch-up completed status=%s datasets=%s", result.get("status"), result.get("datasets"))
        _heartbeat(status="idle", ready=True, last_job_finished_at=datetime.now(timezone.utc).isoformat(), last_job_status=result.get("status"), last_error_code=None)
    except Exception as exc:
        logger.error("catch-up failed code=%s", getattr(exc, "code", "UNEXPECTED"))
        _heartbeat(status="idle", ready=True, last_job_finished_at=datetime.now(timezone.utc).isoformat(), last_job_status="FAILED", last_error_code=getattr(exc, "code", "UNEXPECTED"))
    finally:
        db.close()


def main() -> None:
    _heartbeat(status="starting", ready=False, scheduler_started_at=None)
    start_health_server(Path(settings.worker_heartbeat_file))
    init_db()
    db = SessionLocal()
    seed_score_version(db)
    db.close()
    run_catch_up()
    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(run_catch_up, CronTrigger(day_of_week="mon-fri", hour=21, minute=30, timezone=settings.timezone), id="main-sync", replace_existing=True)
    scheduler.add_job(run_catch_up, CronTrigger(day_of_week="mon-fri", hour=23, minute=0, timezone=settings.timezone), id="retry-sync", replace_existing=True)
    logger.info("worker scheduled timezone=%s", settings.timezone)
    _heartbeat(status="idle", ready=True, scheduler_started_at=datetime.now(timezone.utc).isoformat())
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
