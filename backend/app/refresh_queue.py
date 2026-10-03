"""Shared, serialized queue creation for manual and hourly 3,500-request runs."""
from datetime import date, datetime, timezone
from threading import Lock
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session
from .calendar import expected_trading_sessions
from .models import AccumulationScore, BrokerDaily, ForeignShareholdingDaily, HoldingDistribution, InstitutionalDaily, JobRun, PriceDaily, Stock
from .ingestion import UNIVERSE_BUDGET_LIMIT, UNIVERSE_BUDGET_REFRESH_DATASET, skipped_refresh_stock_ids
from .scoring import SCORE_VERSION
from .refresh_completion import completion_summary, daily_refresh_completion

ACTIVE_STATUSES = ("QUEUED", "RUNNING", "WAITING_FOR_QUOTA", "WAITING_FOR_PROVIDER")
_queue_lock = Lock()

PARTIAL_SOURCE_SPECS = {
    "institutional": (InstitutionalDaily, "TaiwanStockInstitutionalInvestorsBuySellWide"),
    "foreign_holding": (ForeignShareholdingDaily, "TaiwanStockShareholding"),
    "holding_distribution": (HoldingDistribution, "TaiwanStockHoldingSharesPer"),
    "broker": (BrokerDaily, "TaiwanStockTradingDailyReport"),
    "price": (PriceDaily, "TaiwanStockPrice"),
}


def _universe_budget_queue(db: Session) -> tuple[list[str], dict[str, str | None], int]:
    stocks = list(db.scalars(select(Stock).where(Stock.is_common_stock.is_(True)).order_by(Stock.stock_id)).all())
    skipped = skipped_refresh_stock_ids(db)
    latest_scores = {
        str(stock_id): score
        for stock_id, score in db.execute(
            select(AccumulationScore.stock_id, AccumulationScore.score)
            .where(AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None), AccumulationScore.score.is_not(None))
            .order_by(AccumulationScore.stock_id, AccumulationScore.source_date.desc(), AccumulationScore.calculated_at.desc(), AccumulationScore.id.desc())
        ).all()
        if str(stock_id) not in skipped
    }
    latest_fetch: dict[str, datetime] = {}
    for model, dataset in PARTIAL_SOURCE_SPECS.values():
        for stock_id, fetched_at in db.execute(
            select(model.stock_id, func.max(model.fetched_at))
            .where(model.source_dataset == dataset)
            .group_by(model.stock_id)
        ).all():
            if fetched_at is not None and (str(stock_id) not in latest_fetch or fetched_at > latest_fetch[str(stock_id)]):
                latest_fetch[str(stock_id)] = fetched_at
    eligible = [stock.stock_id for stock in stocks if stock.stock_id not in skipped]
    ordered = sorted(
        eligible,
        key=lambda stock_id: (
            0 if stock_id not in latest_fetch and latest_scores.get(stock_id) is None else 1,
            latest_fetch[stock_id].isoformat() if stock_id in latest_fetch else "",
            stock_id,
        ),
    )
    return ordered, {stock_id: latest_fetch[stock_id].isoformat() if stock_id in latest_fetch else None for stock_id in ordered}, len(skipped)


def queue_universe_budget_refresh(db: Session, target: date, *, automatic: bool = False, now: datetime | None = None) -> tuple[JobRun, bool]:
    target = expected_trading_sessions(target, 1)[-1]
    # PostgreSQL serializes the API and worker across processes until commit.
    with _queue_lock:
        try:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(8202609083500)"))
            return _queue_unlocked(db, target, automatic=automatic, now=now)
        except Exception:
            db.rollback()
            raise


def _queue_unlocked(db: Session, target: date, *, automatic: bool, now: datetime | None) -> tuple[JobRun, bool]:
    schedule_hour = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat() if automatic else None
    criteria = JobRun.status.in_(ACTIVE_STATUSES)
    if automatic:
        criteria = or_(criteria, JobRun.checkpoint_state["schedule_hour"].as_string() == schedule_hour)
    job = db.scalar(select(JobRun).where(JobRun.dataset == UNIVERSE_BUDGET_REFRESH_DATASET, criteria).order_by(JobRun.id.desc()).limit(1))
    if job is not None and automatic and job.status not in ACTIVE_STATUSES and (job.checkpoint_state or {}).get("phase") == "daily_target_completed":
        # A restored stock can invalidate a completed target even in the same hour.
        if not daily_refresh_completion(db, target)["all_complete"]:
            job = None
    if job is not None:
        db.commit()
        return job, False
    stock_ids, latest_fetch, skipped_count = _universe_budget_queue(db)
    completion = daily_refresh_completion(db, target) if automatic else None
    if completion is not None:
        pending = set(completion["pending_stock_ids"])
        stock_ids = [sid for sid in stock_ids if sid in pending]
        if completion["all_complete"]:
            previous = db.scalar(select(JobRun).where(JobRun.dataset == UNIVERSE_BUDGET_REFRESH_DATASET, JobRun.requested_end_date == target, JobRun.checkpoint_state["trigger"].as_string() == "closed_market_hourly").order_by(JobRun.id.desc()).limit(1))
            if previous is not None and (previous.checkpoint_state or {}).get("phase") == "daily_target_completed":
                # Keep the historical exclusion/completion snapshot intact.
                # The exclusion endpoint reports live completion separately.
                db.commit()
                return previous, False
    if not stock_ids and not (completion and completion["all_complete"]):
        raise ValueError("NO_ELIGIBLE_STOCKS")
    done = bool(completion and completion["all_complete"])
    job = JobRun(
        dataset=UNIVERSE_BUDGET_REFRESH_DATASET,
        requested_date=target,
        requested_start_date=target,
        requested_end_date=target,
        status="SUCCESS" if done else "QUEUED",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc) if done else None,
        stocks_attempted=len(stock_ids),
        checkpoint_state={
            "run_mode": "universe_fixed_budget_refresh_and_score",
            "trigger": "closed_market_hourly" if automatic else "manual",
            "schedule_hour": schedule_hour,
            "target_date": target.isoformat(),
            "phase": "daily_target_completed" if done else "queued",
            "daily_completion": completion_summary(completion) if completion else None,
            "stock_ids": stock_ids,
            "cycle_stock_ids": stock_ids,
            "ordered_latest_fetch_at": latest_fetch,
            "queue_index": 0,
            "cycle": 1,
            "stocks_completed": 0,
            "current_stock_id": None,
            "current_stock_progress": {},
            "next_retry_at": None,
            "skipped_no_data_count": skipped_count,
            "budget": {"limit": UNIVERSE_BUDGET_LIMIT, "used": 0, "remaining": UNIVERSE_BUDGET_LIMIT},
        },
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job, True
