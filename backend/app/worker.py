from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_settings
from .db import SessionLocal, init_db
from .finmind import FinMindClient
from .ingestion import catch_up, seed_score_version

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


def run_catch_up() -> None:
    db = SessionLocal()
    try:
        result = asyncio.run(catch_up(db, FinMindClient(settings)))
        logger.info("catch-up completed status=%s datasets=%s", result.get("status"), result.get("datasets"))
    except Exception as exc:
        logger.error("catch-up failed code=%s", getattr(exc, "code", "UNEXPECTED"))
    finally:
        db.close()


def main() -> None:
    init_db()
    db = SessionLocal()
    seed_score_version(db)
    db.close()
    run_catch_up()
    scheduler = BlockingScheduler(timezone=settings.timezone)
    scheduler.add_job(run_catch_up, CronTrigger(day_of_week="mon-fri", hour=21, minute=30, timezone=settings.timezone), id="main-sync", replace_existing=True)
    scheduler.add_job(run_catch_up, CronTrigger(day_of_week="mon-fri", hour=23, minute=0, timezone=settings.timezone), id="retry-sync", replace_existing=True)
    logger.info("worker scheduled timezone=%s", settings.timezone)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()

