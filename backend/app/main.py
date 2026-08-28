from __future__ import annotations

import asyncio
import logging
import json
import re
from threading import Lock
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import and_, asc, case, desc, func, nulls_last, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal, get_db, init_db
from .finmind import GLOBAL_PROVIDER_FAILURE_CODES
from .features import build_features, holding_distribution_features
from .ingestion import TARGETED_STOCK_SYNC_DATASET, authoritative_expected_latest_source_date, authoritative_source_state_hash, evaluate_stock_readiness, evaluate_universe_readiness, fetch_and_score_stock, score_existing_data, score_snapshot_state, seed_score_version
from .models import AccumulationFeature, AccumulationScore, BrokerDaily, DataSyncStatus, ForeignShareholdingDaily, HoldingDistribution, InstitutionalDaily, JobRun, PriceDaily, Stock
from .schemas import PaginatedStocks, StockListItem
from .calendar import CALENDAR_HASH, CALENDAR_VERSION
from .scoring import FORMULA_HASH, SCORE_MANIFEST, SCORE_VERSION
from .worker_health import evaluate_health

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()
BUILD_METADATA_PATH = Path("/app/build-metadata.json")
HEX_40 = re.compile(r"^[a-f0-9]{40}$")
HEX_64 = re.compile(r"^[a-f0-9]{64}$")
app = FastAPI(title=settings.app_name, version="0.1.0", docs_url="/api/docs", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])

CURRENT_SCORE_DATASETS = (
    "TaiwanStockInfo",
    "TaiwanStockInstitutionalInvestorsBuySellWide",
    "TaiwanStockShareholding",
    "TaiwanStockHoldingSharesPer",
    "TaiwanStockTradingDailyReport",
    "TaiwanStockPrice",
)
HOLDING_DISTRIBUTION_DATASET = "TaiwanStockHoldingSharesPer"

PARTIAL_SOURCE_SPECS = {
    "institutional": (InstitutionalDaily, "TaiwanStockInstitutionalInvestorsBuySellWide"),
    "foreign_holding": (ForeignShareholdingDaily, "TaiwanStockShareholding"),
    "holding_distribution": (HoldingDistribution, "TaiwanStockHoldingSharesPer"),
    "broker": (BrokerDaily, "TaiwanStockTradingDailyReport"),
    "price": (PriceDaily, "TaiwanStockPrice"),
}
_MANUAL_SCORE_LOCK = Lock()
_TARGETED_SCORE_LOCK = Lock()


@app.on_event("startup")
def startup() -> None:
    init_db()
    with next(get_db()) as db:
        seed_score_version(db)


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        db.execute(select(func.count()).select_from(Stock)).scalar_one()
        metadata = _load_build_metadata()
        if settings.app_env == "production" and metadata["build_metadata_available"] is not True:
            raise HTTPException(status_code=503, detail={"status": "degraded", "database": "ok", "build_metadata": metadata.get("error_code")})
        return {"status": "ok", "service": "api", "database": "ok", "score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH, "timezone": settings.timezone}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail={"status": "degraded", "database": "unavailable"})


@app.get("/api/score-spec")
def score_spec() -> dict[str, Any]:
    return {"score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH, "calendar_version": CALENDAR_VERSION, "spec": SCORE_MANIFEST}


@app.get("/api/build-metadata")
def build_metadata() -> dict[str, Any]:
    return _load_build_metadata()


def _load_build_metadata(path: Path | None = None) -> dict[str, Any]:
    metadata_path = path or BUILD_METADATA_PATH
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {"source_revision": "development", "build_metadata_available": False, "error_code": "BUILD_METADATA_MISSING_OR_INVALID"}
    if not isinstance(payload, dict):
        return {"source_revision": "development", "build_metadata_available": False, "error_code": "BUILD_METADATA_NOT_OBJECT"}
    required = {"source_revision", "backend_lock_sha256", "score_spec_hash", "calendar_hash", "build_timestamp"}
    if missing := sorted(required - set(payload)):
        return {**payload, "build_metadata_available": False, "error_code": "BUILD_METADATA_FIELDS_MISSING", "missing_fields": missing}
    valid_timestamp = False
    try:
        datetime.fromisoformat(str(payload["build_timestamp"]).replace("Z", "+00:00"))
        valid_timestamp = True
    except ValueError:
        pass
    hashes_valid = HEX_40.fullmatch(str(payload["source_revision"])) is not None and HEX_64.fullmatch(str(payload["backend_lock_sha256"])) is not None
    bound = payload["score_spec_hash"] == FORMULA_HASH and payload["calendar_hash"] == CALENDAR_HASH
    if not (hashes_valid and valid_timestamp and bound):
        return {**payload, "build_metadata_available": False, "error_code": "BUILD_METADATA_PROVENANCE_MISMATCH", "score_spec_match": payload.get("score_spec_hash") == FORMULA_HASH, "calendar_match": payload.get("calendar_hash") == CALENDAR_HASH}
    return {**payload, "build_metadata_available": True, "error_code": None, "score_spec_match": True, "calendar_match": True}


@app.get("/api/worker-health")
def worker_health() -> dict[str, Any]:
    path = Path(settings.worker_heartbeat_file)
    try:
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, ValueError):
        return {"status": "degraded", "ready": False, "reason": "heartbeat_missing"}
    result = evaluate_health(heartbeat)
    result["age_seconds"] = result.get("heartbeat_age_seconds")
    result["overdue"] = result.get("stale")
    return result


@app.get("/api/summary")
def summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    sync = db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all()
    provider_state = _provider_state(sync)
    current_latest = _current_score_date(db, provider_state, sync)
    historical_latest = _latest_score_date(db)
    total = db.scalar(select(func.count()).select_from(Stock).where(Stock.is_common_stock.is_(True))) or 0
    if current_latest is not None:
        _, status_map = _canonical_statuses(db, current_latest)
        counts = {status: sum(1 for value in status_map.values() if value == status) for status in ("STRONG_ACCUMULATION", "ACCUMULATION", "WATCH", "DATA_INSUFFICIENT", "NO_STRONG_EVIDENCE")}
    else:
        counts = {"STRONG_ACCUMULATION": 0, "ACCUMULATION": 0, "WATCH": 0, "DATA_INSUFFICIENT": total, "NO_STRONG_EVIDENCE": 0}
    last_updates = [s.last_fetch_at or s.last_successful_sync for s in sync if s.last_fetch_at or s.last_successful_sync]
    score_job = db.scalar(select(JobRun).where(JobRun.dataset == "score").order_by(JobRun.finished_at.desc(), JobRun.id.desc()).limit(1))
    score_metrics = (score_job.checkpoint_state or {}).get("score_metrics", {}) if score_job else {}
    return {"stock_count": total, "strong_count": counts["STRONG_ACCUMULATION"], "accumulation_count": counts["ACCUMULATION"], "watch_count": counts["WATCH"], "data_insufficient_count": counts["DATA_INSUFFICIENT"], "no_strong_evidence_count": counts["NO_STRONG_EVIDENCE"], "status_invariant": sum(counts.values()) == total, "latest_score_date": current_latest, "historical_latest_score_date": historical_latest, "score_ready": current_latest is not None, "historical_score_blocked": provider_state.get("score_blocked") is True, "score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH, "last_data_update": max(last_updates, default=None), "provider_state": provider_state, "score_metrics": score_metrics, "sync_status": [_sync_dict(s) for s in sync]}


@app.get("/api/readiness")
def readiness(source_date: date | None = Query(None), stock_id: str | None = Query(None, max_length=16), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Deterministic per-stock readiness audit; never writes score rows."""
    target = _current_data_date(db, source_date)
    if stock_id:
        stock = db.get(Stock, stock_id)
        if stock is None or not stock.is_common_stock:
            raise HTTPException(status_code=404, detail="stock not found")
        return evaluate_stock_readiness(db, stock_id, target)
    stock_ids = list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True)).order_by(Stock.stock_id)).all())
    return evaluate_universe_readiness(db, stock_ids, target)


def _current_data_date(db: Session, requested: date | None = None) -> date:
    """Choose the current scoring target without asking FinMind for more data."""
    if requested is not None:
        return requested
    expected_dates = [
        expected
        for dataset in CURRENT_SCORE_DATASETS
        if (expected := authoritative_expected_latest_source_date(dataset)) is not None
    ]
    if expected_dates:
        return max(expected_dates)
    persisted_dates = [
        row.latest_source_date
        for row in db.scalars(select(DataSyncStatus).where(DataSyncStatus.dataset.in_(CURRENT_SCORE_DATASETS))).all()
        if row.latest_source_date is not None
    ]
    return max(persisted_dates, default=date.today())


def _score_job_payload(job: JobRun) -> dict[str, Any]:
    checkpoint = job.checkpoint_state if isinstance(job.checkpoint_state, dict) else {}
    metrics = checkpoint.get("score_metrics") if isinstance(checkpoint.get("score_metrics"), dict) else {}
    return {
        "job_id": job.id,
        "status": job.status,
        "run_mode": checkpoint.get("run_mode", "existing_data"),
        "target_date": job.requested_end_date,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "processed_stock_count": checkpoint.get("processed_stock_count", metrics.get("evaluated_stock_count", 0)),
        "universe_stock_count": checkpoint.get("universe_stock_count", metrics.get("universe_stock_count", job.stocks_attempted)),
        "scores": checkpoint.get("scores", {}),
        "score_metrics": metrics,
        "error_code": job.error_code,
    }


def _mark_manual_score_failed(job_id: int, exc: Exception) -> None:
    db = SessionLocal()
    try:
        job = db.get(JobRun, job_id)
        if job is None or job.status != "RUNNING":
            return
        job.status = "FAILED"
        job.finished_at = datetime.now(timezone.utc)
        job.error_code = getattr(exc, "code", "MANUAL_SCORE_FAILED")
        job.error = str(exc)[:500]
        db.commit()
    finally:
        db.close()


def _run_manual_score(job_id: int, target: date) -> None:
    if not _MANUAL_SCORE_LOCK.acquire(blocking=False):
        _mark_manual_score_failed(job_id, RuntimeError("another score job is already running"))
        return
    db = SessionLocal()
    try:
        job = db.get(JobRun, job_id)
        if job is None or job.status != "RUNNING":
            return
        score_existing_data(db, target, job=job)
    except Exception as exc:
        db.rollback()
        _mark_manual_score_failed(job_id, exc)
    finally:
        db.close()
        _MANUAL_SCORE_LOCK.release()


def _targeted_score_job_payload(job: JobRun) -> dict[str, Any]:
    checkpoint = job.checkpoint_state if isinstance(job.checkpoint_state, dict) else {}
    return {
        "job_id": job.id,
        "stock_id": checkpoint.get("stock_id"),
        "status": job.status,
        "run_mode": checkpoint.get("run_mode", "targeted_fetch_and_score"),
        "target_date": job.requested_end_date,
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
    }


def _mark_targeted_score_failed(job_id: int, exc: Exception) -> None:
    db = SessionLocal()
    try:
        job = db.get(JobRun, job_id)
        if job is None or job.status != "RUNNING":
            return
        job.status = "FAILED"
        job.finished_at = datetime.now(timezone.utc)
        job.error_code = getattr(exc, "code", "TARGETED_SCORE_FAILED")
        job.error = str(exc)[:500]
        checkpoint = job.checkpoint_state if isinstance(job.checkpoint_state, dict) else {}
        job.checkpoint_state = {**checkpoint, "phase": "failed"}
        db.commit()
    finally:
        db.close()


def _run_targeted_fetch_and_score(job_id: int, stock_id: str, target: date) -> None:
    if not _TARGETED_SCORE_LOCK.acquire(blocking=False):
        _mark_targeted_score_failed(job_id, RuntimeError("another targeted stock job is already running"))
        return
    db = SessionLocal()
    try:
        job = db.get(JobRun, job_id)
        if job is None or job.status != "RUNNING":
            return
        from .finmind import FinMindClient
        asyncio.run(fetch_and_score_stock(db, FinMindClient(settings), stock_id, target, job=job))
    except Exception as exc:
        db.rollback()
        _mark_targeted_score_failed(job_id, exc)
    finally:
        db.close()
        _TARGETED_SCORE_LOCK.release()


@app.post("/api/score/current", status_code=202)
def start_current_score(background_tasks: BackgroundTasks, source_date: date | None = Query(None), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Queue a score run using only source rows already stored locally."""
    running = db.scalar(
        select(JobRun)
        .where(JobRun.dataset == "score", JobRun.status == "RUNNING")
        .order_by(JobRun.id.desc())
        .limit(1)
    )
    if running is not None:
        raise HTTPException(status_code=409, detail={"code": "SCORE_JOB_ALREADY_RUNNING", "job_id": running.id})
    target = _current_data_date(db, source_date)
    stock_count = db.scalar(select(func.count()).select_from(Stock).where(Stock.is_common_stock.is_(True))) or 0
    job = JobRun(
        dataset="score",
        requested_date=target,
        requested_start_date=target,
        requested_end_date=target,
        status="RUNNING",
        started_at=datetime.now(timezone.utc),
        stocks_attempted=stock_count,
        checkpoint_state={"run_mode": "existing_data", "target_date": target.isoformat(), "processed_stock_count": 0, "universe_stock_count": stock_count, "scores": {}, "score_metrics": {}},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(_run_manual_score, job.id, target)
    return _score_job_payload(job)


@app.get("/api/score/current")
def current_score_status(job_id: int | None = Query(None, ge=1), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return the latest or requested local-only score job status."""
    if job_id is not None:
        job = db.get(JobRun, job_id)
    else:
        job = db.scalar(select(JobRun).where(JobRun.dataset == "score").order_by(JobRun.id.desc()).limit(1))
    if job is None or job.dataset != "score":
        raise HTTPException(status_code=404, detail="score job not found")
    return _score_job_payload(job)


@app.get("/api/stocks", response_model=PaginatedStocks)
def stocks(
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), search: str | None = Query(None, max_length=64), market: str | None = Query(None), industry: str | None = Query(None), status: str | None = Query(None), min_score: float | None = Query(None, ge=0, le=100), sort: str = Query("score"), order: str = Query("desc"),
    db: Session = Depends(get_db),
) -> PaginatedStocks:
    sync = db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all()
    provider_state = _provider_state(sync)
    latest = _current_score_date(db, provider_state, sync)
    score_value, score_status, score_version, score_coverage = _score_subqueries(latest)
    price_value, price_change = _price_subqueries()
    feature_values, feature_date = _feature_subqueries(latest)
    base = select(Stock, score_value, score_status, score_version, score_coverage, price_value, price_change, feature_values, feature_date).where(Stock.is_common_stock.is_(True))
    if search:
        needle = f"%{search.strip()}%"
        base = base.where((Stock.stock_id.ilike(needle)) | (Stock.stock_name.ilike(needle)))
    if market:
        base = base.where(Stock.market == market)
    if industry:
        base = base.where(Stock.industry == industry)
    allowed_sort = {"stock_id": Stock.stock_id, "stock_name": Stock.stock_name, "market": Stock.market, "industry": Stock.industry}
    sort_column = score_value if sort == "score" else allowed_sort.get(sort, Stock.stock_id)
    effective_status = func.coalesce(score_status, "DATA_INSUFFICIENT")
    if latest:
        if status:
            base = base.where(effective_status == status)
    if min_score is not None:
        base = base.where(score_value >= min_score)
    if status and not latest:
        base = base.where(effective_status == status)
    direction = desc(sort_column) if order.lower() == "desc" else asc(sort_column)
    base = base.order_by(nulls_last(direction) if sort == "score" else direction, asc(Stock.stock_id))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(base.offset((page - 1) * page_size).limit(page_size)).all()
    partial_data = _partial_stock_snapshots(db, [row[0].stock_id for row in rows])
    items = [_stock_item_from_row(row, partial_data.get(row[0].stock_id)) for row in rows]
    return PaginatedStocks(items=items, total=total, page=page, page_size=page_size)


@app.get("/api/rankings")
def rankings(kind: str = Query("top"), limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)) -> dict[str, Any]:
    sync = db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all()
    provider_state = _provider_state(sync)
    latest = _current_score_date(db, provider_state, sync)
    if latest is None:
        return {"source_date": None, "score_version": SCORE_VERSION, "provider_state": provider_state, "items": []}
    score_value, score_status, _, score_components = _score_subqueries(latest, components=True)
    query = select(Stock, score_value, score_status, score_components).where(Stock.is_common_stock.is_(True), score_value.is_not(None)).order_by(desc(score_value), asc(Stock.stock_id)).limit(limit)
    rows = db.execute(query).all()
    return {"source_date": latest, "kind": kind, "score_version": SCORE_VERSION, "provider_state": provider_state, "items": [{"stock_id": row[0].stock_id, "stock_name": row[0].stock_name, "market": row[0].market, "score": row[1], "status": row[2], "components": row[3]} for row in rows]}


@app.post("/api/stocks/{stock_id}/fetch-and-score", status_code=202)
def start_targeted_fetch_and_score(stock_id: str, background_tasks: BackgroundTasks, source_date: date | None = Query(None), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Queue a single-stock missing-data fetch followed by immediate scoring."""
    stock = db.get(Stock, stock_id)
    if stock is None or not stock.is_common_stock:
        raise HTTPException(status_code=404, detail="stock not found")
    running = db.scalar(
        select(JobRun)
        .where(JobRun.dataset == TARGETED_STOCK_SYNC_DATASET, JobRun.status == "RUNNING")
        .order_by(JobRun.id.desc())
        .limit(1)
    )
    if running is not None:
        running_stock_id = (running.checkpoint_state or {}).get("stock_id") if isinstance(running.checkpoint_state, dict) else None
        raise HTTPException(status_code=409, detail={"code": "TARGETED_SCORE_JOB_ALREADY_RUNNING", "job_id": running.id, "stock_id": running_stock_id})
    target = _current_data_date(db, source_date)
    job = JobRun(
        dataset=TARGETED_STOCK_SYNC_DATASET,
        requested_date=target,
        requested_start_date=target,
        requested_end_date=target,
        status="RUNNING",
        started_at=datetime.now(timezone.utc),
        stocks_attempted=1,
        checkpoint_state={"run_mode": "targeted_fetch_and_score", "stock_id": stock_id, "target_date": target.isoformat(), "phase": "queued", "progress": {"completed": 0, "total": 5}, "datasets": {}, "fetch_errors": []},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(_run_targeted_fetch_and_score, job.id, stock_id, target)
    return _targeted_score_job_payload(job)


@app.get("/api/stocks/{stock_id}/fetch-and-score")
def targeted_fetch_and_score_status(stock_id: str, job_id: int | None = Query(None, ge=1), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return the latest or requested targeted single-stock job."""
    stock = db.get(Stock, stock_id)
    if stock is None or not stock.is_common_stock:
        raise HTTPException(status_code=404, detail="stock not found")
    if job_id is not None:
        job = db.get(JobRun, job_id)
    else:
        job = db.scalar(select(JobRun).where(JobRun.dataset == TARGETED_STOCK_SYNC_DATASET).order_by(JobRun.id.desc()).limit(1))
    if job is None or job.dataset != TARGETED_STOCK_SYNC_DATASET or (job.checkpoint_state or {}).get("stock_id") != stock_id:
        raise HTTPException(status_code=404, detail="targeted stock job not found")
    return _targeted_score_job_payload(job)


@app.get("/api/stocks/{stock_id}")
def stock_detail(stock_id: str, limit: int = Query(365, ge=1, le=1000), db: Session = Depends(get_db)) -> dict[str, Any]:
    stock = db.get(Stock, stock_id)
    if stock is None:
        raise HTTPException(status_code=404, detail="stock not found")
    sync = db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all()
    provider_state = _provider_state(sync)
    latest = _current_score_date(db, provider_state, sync)
    # A targeted run may have produced a fresh, stock-specific score while
    # the universe-wide snapshot is still blocked by other stocks.  Detail
    # pages should show that score immediately without changing global gate
    # semantics or pretending the whole universe is complete.
    if latest is None:
        targeted_job = db.scalar(
            select(JobRun)
            .where(JobRun.dataset == TARGETED_STOCK_SYNC_DATASET, JobRun.status == "SUCCESS")
            .order_by(JobRun.finished_at.desc(), JobRun.id.desc())
            .limit(1)
        )
        if targeted_job is not None and isinstance(targeted_job.checkpoint_state, dict) and targeted_job.checkpoint_state.get("stock_id") == stock_id:
            latest = targeted_job.requested_end_date
    partial_data = _partial_stock_snapshots(db, [stock_id]).get(stock_id, {})
    stock_payload = {"stock_id": stock.stock_id, "stock_name": stock.stock_name, "market": stock.market, "industry": stock.industry, **{key: partial_data.get(key) for key in ("data_status", "data_latest_source_date", "last_updated_at", "data_sources", "features", "coverage")}}
    return {"stock": stock_payload, "score": _score_dict(db, stock_id, latest), "provider_state": provider_state, "sources": _source_status(db, stock_id), "institutional": _rows(db, InstitutionalDaily, stock_id, min(limit, 365), "TaiwanStockInstitutionalInvestorsBuySellWide"), "foreign_holding": _rows(db, ForeignShareholdingDaily, stock_id, min(limit, 365), "TaiwanStockShareholding"), "holding_distribution": _rows(db, HoldingDistribution, stock_id, min(limit, 200), "TaiwanStockHoldingSharesPer"), "holding_series": _holding_chart_series(db, stock_id, min(limit, 200)), "brokers": _broker_summary(db, stock_id), "prices": _rows(db, PriceDaily, stock_id, min(limit, 365), "TaiwanStockPrice"), "score_history": _score_history(db, stock_id, min(limit, 365)), "calendar_version": CALENDAR_VERSION}


@app.get("/api/holdings/status")
def all_holdings_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return the latest large-holder state for every common stock.

    This is a read of the persisted weekly holding distribution and is
    intentionally independent of the exchange's current trading session.
    The page can therefore hydrate this snapshot once at startup, including
    weekends and outside continuous trading hours.
    """
    stocks = db.scalars(select(Stock).where(Stock.is_common_stock.is_(True)).order_by(Stock.stock_id)).all()
    latest_dates = (
        select(HoldingDistribution.stock_id, func.max(HoldingDistribution.source_date).label("latest_source_date"))
        .where(HoldingDistribution.source_dataset == HOLDING_DISTRIBUTION_DATASET)
        .group_by(HoldingDistribution.stock_id)
        .subquery()
    )
    rows = db.scalars(
        select(HoldingDistribution)
        .join(Stock, Stock.stock_id == HoldingDistribution.stock_id)
        .join(
            latest_dates,
            and_(
                latest_dates.c.stock_id == HoldingDistribution.stock_id,
                latest_dates.c.latest_source_date == HoldingDistribution.source_date,
            ),
        )
        .where(
            Stock.is_common_stock.is_(True),
            HoldingDistribution.source_dataset == HOLDING_DISTRIBUTION_DATASET,
        )
        .order_by(HoldingDistribution.stock_id, HoldingDistribution.holding_shares_threshold, HoldingDistribution.holding_shares_level)
    ).all()
    rows_by_stock: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_stock.setdefault(row.stock_id, []).append(
            {
                "source_date": row.source_date,
                "holding_shares_level": row.holding_shares_level,
                "holding_shares_threshold": row.holding_shares_threshold,
                "people": row.people,
                "percent": row.percent,
                "shares": row.shares,
            }
        )

    items: list[dict[str, Any]] = []
    for stock in stocks:
        features = holding_distribution_features(rows_by_stock.get(stock.stock_id, []))
        coverage = features.get("HoldingDistributionCoverage") or {}
        available = coverage.get("available") is True
        items.append(
            {
                "stock_id": stock.stock_id,
                "stock_name": stock.stock_name,
                "market": stock.market,
                "latest_source_date": features.get("HoldingDistributionLatestDate"),
                "status": "AVAILABLE" if available else "DATA_INSUFFICIENT",
                "large_holder_400_lots_percent": features.get("LargeHolder400LotsPercent"),
                "large_holder_400_lots_people": features.get("LargeHolder400LotsPeople"),
                "large_holder_1000_lots_percent": features.get("LargeHolder1000LotsPercent"),
                "large_holder_1000_lots_people": features.get("LargeHolder1000LotsPeople"),
            }
        )

    return {
        "dataset": HOLDING_DISTRIBUTION_DATASET,
        "market_session_required": False,
        "total": len(items),
        "available_count": sum(1 for item in items if item["status"] == "AVAILABLE"),
        "items": items,
    }


@app.get("/api/stocks/{stock_id}/institutional")
def institutional(stock_id: str, limit: int = Query(365, ge=1, le=1000), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _rows(db, InstitutionalDaily, stock_id, limit, "TaiwanStockInstitutionalInvestorsBuySellWide")


@app.get("/api/stocks/{stock_id}/foreign-holding")
def foreign_holding(stock_id: str, limit: int = Query(365, ge=1, le=1000), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _rows(db, ForeignShareholdingDaily, stock_id, limit, "TaiwanStockShareholding")


@app.get("/api/stocks/{stock_id}/holding-distribution")
def holding_distribution(stock_id: str, limit: int = Query(200, ge=1, le=500), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _rows(db, HoldingDistribution, stock_id, limit, "TaiwanStockHoldingSharesPer")


@app.get("/api/stocks/{stock_id}/brokers")
def brokers(stock_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _broker_summary(db, stock_id)


@app.get("/api/stocks/{stock_id}/score-history")
def score_history(stock_id: str, limit: int = Query(365, ge=1, le=1000), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _score_history(db, stock_id, limit)


@app.get("/api/data-status")
def data_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all()
    jobs = db.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(50)).all()
    provider_state = _provider_state(rows)
    latest_score_date = _current_score_date(db, provider_state, rows)
    return {"provider_state": provider_state, "score_ready": latest_score_date is not None, "latest_score_date": latest_score_date, "datasets": [_sync_dict(row) for row in rows], "jobs": [{"dataset": j.dataset, "status": j.status, "requested_date": j.requested_date, "requested_start_date": j.requested_start_date, "requested_end_date": j.requested_end_date, "started_at": j.started_at, "finished_at": j.finished_at, "records": j.records, "duration_ms": j.duration_ms, "retry_count": j.retry_count, "stocks_attempted": j.stocks_attempted, "stocks_completed": j.stocks_completed, "stocks_failed": j.stocks_failed, "checkpoint_state": j.checkpoint_state, "error_code": j.error_code, "error": j.error} for j in jobs]}


def _latest_score_date(db: Session) -> date | None:
    return db.scalar(select(func.max(AccumulationScore.source_date)).where(AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None)))


def _current_score_date(db: Session, provider_state: dict[str, Any], sync: list[DataSyncStatus] | None = None) -> date | None:
    """Return a current-date score bound to a per-stock scoring run.

    Source-level completeness is intentionally advisory here.  A mixed score
    run is a valid current snapshot when at least one stock passed the same
    point-in-time readiness contract used by the worker.
    """
    sync = sync if sync is not None else list(db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all())
    readiness = _current_score_readiness(db, provider_state, sync)
    source_gate = provider_state.get("numeric_scores_allowed") is True
    provider_state["source_coverage_numeric_scores_allowed"] = source_gate
    provider_state["numeric_scores_allowed"] = readiness["ready"]
    provider_state["score_readiness"] = readiness
    provider_state["score_ready"] = readiness["ready"]
    provider_state["score_blocked"] = not readiness["ready"]
    provider_state["score_blocking_reason"] = None if readiness["ready"] else "SCORE_BLOCKED_BY_SOURCE_COVERAGE"
    if not readiness["ready"]:
        if source_gate:
            provider_state["status"] = "DATA_INSUFFICIENT"
            provider_state["reason_code"] = readiness["reason_code"]
        return None
    return date.fromisoformat(str(readiness["target_date"]))


def _current_score_readiness(db: Session, provider_state: dict[str, Any], sync: list[DataSyncStatus]) -> dict[str, Any]:
    expected_dates = [expected for row in sync if row.dataset in CURRENT_SCORE_DATASETS if (expected := authoritative_expected_latest_source_date(row.dataset)) is not None]
    if not expected_dates:
        return {"ready": False, "reason_code": "CURRENT_SCORE_TARGET_DATE_MISSING", "target_date": None}
    target = max(expected_dates)
    job = db.scalar(
        select(JobRun)
        .where(JobRun.dataset == "score", JobRun.requested_end_date == target, JobRun.status.in_(("SUCCESS", "PARTIAL", "REUSED", "SCORE_BLOCKED_BY_SOURCE_COVERAGE")))
        .order_by(JobRun.finished_at.desc(), JobRun.id.desc())
        .limit(1)
    )
    if job is None:
        return {"ready": False, "reason_code": "CURRENT_SCORE_JOB_NOT_SUCCESSFUL", "target_date": target.isoformat()}
    checkpoint = job.checkpoint_state or {}
    score_metrics = checkpoint.get("score_metrics") if isinstance(checkpoint.get("score_metrics"), dict) else {}
    numeric_score_count = db.scalar(select(func.count()).select_from(AccumulationScore).where(AccumulationScore.source_date == target, AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None), AccumulationScore.score.is_not(None))) or 0
    score_rows_count = db.scalar(select(func.count()).select_from(AccumulationScore).where(AccumulationScore.source_date == target, AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None))) or 0
    if numeric_score_count == 0:
        return {"ready": False, "reason_code": "NO_READY_STOCK_SCORES", "target_date": target.isoformat(), "score_rows_count": score_rows_count, "numeric_score_count": numeric_score_count, "score_metrics": score_metrics}
    current_source_hash = authoritative_source_state_hash(db)
    current_scores = score_snapshot_state(db, target)
    stock_count = db.scalar(select(func.count()).select_from(Stock).where(Stock.is_common_stock.is_(True))) or 0
    checks = {
        "target_date_match": checkpoint.get("target_date") == target.isoformat(),
        "score_version_match": checkpoint.get("score_version") == SCORE_VERSION,
        "formula_hash_match": checkpoint.get("formula_hash") == FORMULA_HASH,
        "calendar_hash_match": checkpoint.get("calendar_hash") == CALENDAR_HASH,
        "source_state_hash_match": checkpoint.get("source_state_hash") == current_source_hash,
        "score_snapshot_hash_match": checkpoint.get("score_snapshot_hash") == current_scores["score_snapshot_hash"],
        "stock_count_match": int(checkpoint.get("stock_count", stock_count)) == stock_count,
        "score_rows_formula_match": current_scores["formula_hashes_match"] is True,
        "score_rows_input_bound": current_scores["input_snapshots_bound"] is True,
    }
    # A mixed run intentionally has fewer numeric score rows than the whole
    # universe.  Binding checks still protect version/PIT provenance, while
    # per-stock readiness is represented by the persisted score rows.
    checks["score_snapshot_hash_match"] = current_scores["score_snapshot_hash"] == checkpoint.get("score_snapshot_hash", current_scores["score_snapshot_hash"])
    ready = all(value for key, value in checks.items() if key != "stock_count_match")
    return {"ready": ready, "reason_code": None if ready else "CURRENT_SCORE_RUN_BINDING_FAILED", "target_date": target.isoformat(), "job_run_id": job.id, "checks": checks, "source_state_hash": current_source_hash, "score_rows_count": score_rows_count, "numeric_score_count": numeric_score_count, "score_metrics": score_metrics, **current_scores}


def _canonical_statuses(db: Session, latest: date | None = None) -> tuple[int, dict[str, str]]:
    stock_ids = list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True))).all())
    latest = latest or _latest_score_date(db)
    if latest is None:
        return len(stock_ids), {stock_id: "DATA_INSUFFICIENT" for stock_id in stock_ids}
    scores = db.scalars(select(AccumulationScore).where(AccumulationScore.source_date == latest, AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None)).order_by(AccumulationScore.calculated_at.desc())).all()
    statuses = {stock_id: "DATA_INSUFFICIENT" for stock_id in stock_ids}
    seen: set[str] = set()
    for score in scores:
        if score.stock_id not in seen:
            statuses[score.stock_id] = score.status
            seen.add(score.stock_id)
    return len(stock_ids), statuses


def _score_subqueries(latest: date | None, components: bool = False):
    def field(name: str):
        if latest is None:
            return select(func.cast(None, getattr(AccumulationScore, name).type)).scalar_subquery()
        return select(getattr(AccumulationScore, name)).where(AccumulationScore.stock_id == Stock.stock_id, AccumulationScore.source_date == latest, AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None)).order_by(AccumulationScore.calculated_at.desc()).limit(1).correlate(Stock).scalar_subquery()
    return field("score"), field("status"), field("score_version"), field("components" if components else "coverage")


def _price_subqueries():
    dataset = "TaiwanStockPrice"
    latest_date = select(func.max(PriceDaily.source_date)).where(PriceDaily.stock_id == Stock.stock_id, PriceDaily.source_dataset == dataset).correlate(Stock).scalar_subquery()
    return (select(PriceDaily.close).where(PriceDaily.stock_id == Stock.stock_id, PriceDaily.source_date == latest_date, PriceDaily.source_dataset == dataset).limit(1).correlate(Stock).scalar_subquery(), select(PriceDaily.change).where(PriceDaily.stock_id == Stock.stock_id, PriceDaily.source_date == latest_date, PriceDaily.source_dataset == dataset).limit(1).correlate(Stock).scalar_subquery())


def _feature_subqueries(latest: date | None):
    if latest is None:
        return select(func.json_object()).scalar_subquery(), select(func.cast(None, AccumulationFeature.latest_source_date.type)).scalar_subquery()
    return (select(AccumulationFeature.values).where(AccumulationFeature.stock_id == Stock.stock_id, AccumulationFeature.source_date == latest).order_by(AccumulationFeature.calculated_at.desc()).limit(1).correlate(Stock).scalar_subquery(), select(AccumulationFeature.latest_source_date).where(AccumulationFeature.stock_id == Stock.stock_id, AccumulationFeature.source_date == latest).order_by(AccumulationFeature.calculated_at.desc()).limit(1).correlate(Stock).scalar_subquery())


def _stock_item_from_row(row: Any, partial_data: dict[str, Any] | None = None) -> StockListItem:
    stock, score, status, score_version, coverage, price, price_change, features, latest_data = row
    raw_features = _json_dict(features)
    raw_coverage = _json_dict(coverage)
    partial_data = partial_data or {}
    if not raw_features:
        raw_features = partial_data.get("features", {})
    if not raw_coverage:
        raw_coverage = partial_data.get("coverage", {})
    latest_source_date = latest_data or partial_data.get("data_latest_source_date")
    latest_source_date = latest_source_date.isoformat() if isinstance(latest_source_date, (date, datetime)) else latest_source_date
    partial_latest_source_date = partial_data.get("data_latest_source_date")
    partial_latest_source_date = partial_latest_source_date.isoformat() if isinstance(partial_latest_source_date, (date, datetime)) else partial_latest_source_date
    return StockListItem(stock_id=stock.stock_id, stock_name=stock.stock_name, market=stock.market, industry=stock.industry, price=price, price_change=price_change, score=score, status=status or "DATA_INSUFFICIENT", score_version=score_version, features=raw_features, coverage=raw_coverage, latest_data=latest_source_date, data_status=partial_data.get("data_status", "NO_DATA"), data_latest_source_date=partial_latest_source_date, last_updated_at=partial_data.get("last_updated_at"), data_sources=partial_data.get("data_sources", {}))


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _partial_stock_snapshots(db: Session, stock_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Build display-only features from whatever rows are already persisted.

    This deliberately does not create Score rows.  A stock can therefore show
    useful partial source data while the global Score surface remains
    fail-closed until every required source is current and complete.
    """
    unique_ids = list(dict.fromkeys(stock_ids))
    if not unique_ids:
        return {}
    rows_by_source: dict[str, dict[str, list[dict[str, Any]]]] = {
        name: {stock_id: [] for stock_id in unique_ids} for name in PARTIAL_SOURCE_SPECS
    }
    source_totals: dict[str, dict[str, tuple[date | None, datetime | None, int]]] = {name: {} for name in PARTIAL_SOURCE_SPECS}
    for name, (model, dataset) in PARTIAL_SOURCE_SPECS.items():
        source_totals[name] = {
            str(stock_id): (latest_source_date, latest_fetched_at, int(row_count))
            for stock_id, latest_source_date, latest_fetched_at, row_count in db.execute(
                select(model.stock_id, func.max(model.source_date), func.max(model.fetched_at), func.count())
                .where(model.stock_id.in_(unique_ids), model.source_dataset == dataset)
                .group_by(model.stock_id)
            ).all()
        }
        # Ninety calendar days cover the 20-session windows; holdings need
        # extra room for the eight-week comparison periods. Keep the list
        # endpoint bounded even when the database retains long history.
        lookback_days = 180 if name == "holding_distribution" else 90
        cutoff = date.today() - timedelta(days=lookback_days)
        rows = db.scalars(
            select(model)
            .where(model.stock_id.in_(unique_ids), model.source_dataset == dataset, model.source_date >= cutoff)
            .order_by(model.stock_id, model.source_date.asc(), model.id.asc())
        ).all()
        for row in rows:
            payload = {key: getattr(row, key) for key in row.__table__.columns.keys() if key != "id"}
            rows_by_source[name].setdefault(row.stock_id, []).append(payload)

    result: dict[str, dict[str, Any]] = {}
    coverage_keys = {
        "institutional": "InstitutionalDataAvailable",
        "foreign_holding": "ForeignHoldingDataAvailable",
        "holding_distribution": "HoldingDistributionAvailable",
        "broker": "BrokerDataAvailable",
        "price": "PriceDataAvailable",
    }
    for stock_id in unique_ids:
        source_rows = {name: rows_by_source[name].get(stock_id, []) for name in PARTIAL_SOURCE_SPECS}
        features = build_features(
            source_rows["institutional"],
            source_rows["foreign_holding"],
            source_rows["holding_distribution"],
            source_rows["broker"],
            source_rows["price"],
        )
        data_sources: dict[str, Any] = {}
        source_dates: list[date] = []
        fetched_at: list[datetime] = []
        raw_coverage: dict[str, bool] = {}
        for name, rows in source_rows.items():
            latest_source_date, latest_fetched_at, row_count = source_totals[name].get(stock_id, (None, None, 0))
            if latest_source_date:
                source_dates.append(latest_source_date)
            if latest_fetched_at:
                fetched_at.append(latest_fetched_at)
            available = stock_id in source_totals[name]
            raw_coverage[coverage_keys[name]] = available
            data_sources[name] = {
                "dataset": PARTIAL_SOURCE_SPECS[name][1],
                "available": available,
                "row_count": row_count,
                "latest_source_date": latest_source_date,
                "last_updated_at": latest_fetched_at,
            }
        available_count = sum(raw_coverage.values())
        result[stock_id] = {
            "features": features,
            "coverage": {**raw_coverage, "raw_source_count": available_count, "raw_source_complete": available_count == len(PARTIAL_SOURCE_SPECS)},
            "data_status": "NO_DATA" if available_count == 0 else ("COMPLETE" if available_count == len(PARTIAL_SOURCE_SPECS) else "PARTIAL"),
            "data_latest_source_date": max(source_dates, default=None),
            "last_updated_at": max(fetched_at, default=None),
            "data_sources": data_sources,
        }
    return result


def _score_dict(db: Session, stock_id: str, latest: date | None) -> dict[str, Any]:
    if latest is None:
        return {"score": None, "status": "DATA_INSUFFICIENT", "score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH, "coverage": {}, "explanation": [{"label": "資料不足", "value": 0, "detail": "尚未建立可追溯的 S-level score snapshot"}], "input_source_hashes": []}
    score = db.scalar(select(AccumulationScore).where(AccumulationScore.stock_id == stock_id, AccumulationScore.source_date == latest, AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None)).order_by(AccumulationScore.calculated_at.desc()).limit(1))
    if not score:
        return {"score": None, "status": "DATA_INSUFFICIENT", "score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH, "coverage": {}, "explanation": [{"label": "資料不足", "value": 0, "detail": "required source coverage is incomplete; no zero substitution"}], "input_source_hashes": []}
    return {"score": score.score, "status": score.status, "score_version": score.score_version, "formula_hash": score.formula_hash or FORMULA_HASH, "components": score.components, "explanation": score.explanation, "coverage": score.coverage, "source_date": score.source_date, "calculated_at": score.calculated_at, "knowledge_cutoff": score.knowledge_cutoff, "input_snapshot_hash": score.input_snapshot_hash, "input_source_hashes": score.input_source_hashes}


def _rows(db: Session, model: type[Any], stock_id: str, limit: int, source_dataset: str) -> list[dict[str, Any]]:
    rows = db.scalars(select(model).where(model.stock_id == stock_id, model.source_dataset == source_dataset).order_by(model.source_date.desc()).limit(limit)).all()
    return [{key: getattr(row, key) for key in model.__table__.columns.keys() if key not in {"id", "stock_id"}} | {"stock_id": stock_id} for row in reversed(rows)]


def _broker_summary(db: Session, stock_id: str) -> list[dict[str, Any]]:
    latest_dates = db.scalars(
        select(BrokerDaily.source_date)
        .where(BrokerDaily.stock_id == stock_id, BrokerDaily.source_dataset == "TaiwanStockTradingDailyReport")
        .distinct()
        .order_by(BrokerDaily.source_date.desc())
        .limit(20)
    ).all()
    rows = db.execute(
        select(
            BrokerDaily.securities_trader_id,
            func.max(BrokerDaily.securities_trader_name),
            func.sum(BrokerDaily.buy_volume),
            func.sum(BrokerDaily.sell_volume),
            func.sum(BrokerDaily.net_volume),
            func.sum(case((BrokerDaily.net_volume > 0, 1), else_=0)),
            func.sum(case((BrokerDaily.net_volume < 0, 1), else_=0)),
            func.sum(case((BrokerDaily.net_volume.is_(None), 1), else_=0)),
            func.sum(case((BrokerDaily.buy_volume.is_(None), 1), else_=0)),
            func.sum(case((BrokerDaily.sell_volume.is_(None), 1), else_=0)),
        )
        .where(BrokerDaily.stock_id == stock_id, BrokerDaily.source_dataset == "TaiwanStockTradingDailyReport", BrokerDaily.source_date.in_(latest_dates))
        .group_by(BrokerDaily.securities_trader_id)
        .order_by(desc(func.sum(BrokerDaily.net_volume)), asc(BrokerDaily.securities_trader_id))
        .limit(20)
    ).all()
    return [
        {
            "securities_trader_id": row[0],
            "securities_trader_name": row[1],
            "buy_volume": None if row[8] else row[2],
            "sell_volume": None if row[9] else row[3],
            "net_volume": None if row[7] else row[4],
            "positive_days": row[5],
            "negative_days": row[6],
            "missing_days": row[7],
        }
        for row in rows
    ]


def _score_history(db: Session, stock_id: str, limit: int) -> list[dict[str, Any]]:
    rows = db.scalars(select(AccumulationScore).where(AccumulationScore.stock_id == stock_id, AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None)).order_by(AccumulationScore.source_date.desc(), AccumulationScore.calculated_at.desc()).limit(limit)).all()
    return [{"source_date": row.source_date, "score": row.score, "status": row.status, "components": row.components, "formula_hash": row.formula_hash, "input_snapshot_hash": row.input_snapshot_hash, "calculated_at": row.calculated_at, "knowledge_cutoff": row.knowledge_cutoff} for row in reversed(rows)]


def _source_status(db: Session, stock_id: str) -> dict[str, Any]:
    mapping = {"institutional": (InstitutionalDaily, "TaiwanStockInstitutionalInvestorsBuySellWide"), "foreign_holding": (ForeignShareholdingDaily, "TaiwanStockShareholding"), "holding_distribution": (HoldingDistribution, "TaiwanStockHoldingSharesPer"), "broker": (BrokerDaily, "TaiwanStockTradingDailyReport"), "price": (PriceDaily, "TaiwanStockPrice")}
    result: dict[str, Any] = {}
    for name, (model, dataset) in mapping.items():
        latest = db.scalar(select(func.max(model.source_date)).where(model.stock_id == stock_id, model.source_dataset == dataset))
        fetched = db.scalar(select(func.max(model.fetched_at)).where(model.stock_id == stock_id, model.source_dataset == dataset))
        sync = db.get(DataSyncStatus, dataset)
        expected = authoritative_expected_latest_source_date(dataset) if sync else None
        stock_staleness = "NO_DATA" if latest is None else ("STALE" if expected and latest < expected else "FRESH")
        result[name] = {"provider": "FinMind", "dataset": dataset, "status": stock_staleness, "latest_source_date": latest, "fetched_at": fetched, "last_successful_fetch": sync.last_http_success_at if sync else None, "last_fully_successful_sync": sync.last_fully_successful_sync if sync else None, "last_usable_data_at": sync.last_usable_data_at if sync else None, "attempt_latest_source_date": sync.attempt_latest_source_date if sync else None, "expected_latest_source_date": expected, "source_age_days": (expected - latest).days if expected and latest else None, "row_count": db.scalar(select(func.count()).select_from(model).where(model.stock_id == stock_id, model.source_dataset == dataset)) or 0, "staleness": stock_staleness, "global_sync_staleness": sync.staleness_state if sync else "UNKNOWN", "fallback": "not_used"}
    result["major_shareholder_5pct"] = {"provider": "TWSE/TPEx/MOPS", "dataset": None, "status": "UNAVAILABLE_NOT_CONFIGURED", "fallback": "none"}
    return result


def _holding_chart_series(db: Session, stock_id: str, limit: int) -> dict[str, list[dict[str, Any]]]:
    latest_dates = db.scalars(
        select(HoldingDistribution.source_date)
        .where(HoldingDistribution.stock_id == stock_id, HoldingDistribution.source_dataset == "TaiwanStockHoldingSharesPer")
        .distinct()
        .order_by(HoldingDistribution.source_date.desc())
        .limit(limit)
    ).all()
    rows = db.scalars(select(HoldingDistribution).where(HoldingDistribution.stock_id == stock_id, HoldingDistribution.source_dataset == "TaiwanStockHoldingSharesPer", HoldingDistribution.source_date.in_(latest_dates))).all()
    payload = [{key: getattr(row, key) for key in row.__table__.columns.keys() if key != "id"} for row in rows]
    return holding_distribution_features(payload).get("HoldingDistributionSeries", {"400": [], "1000": []})


def _sync_dict(row: DataSyncStatus) -> dict[str, Any]:
    metadata = row.metadata_json or {}
    expected = authoritative_expected_latest_source_date(row.dataset)
    source_age_days = (expected - row.latest_source_date).days if expected and row.latest_source_date else None
    staleness = row.staleness_state
    if expected and row.latest_source_date and row.latest_source_date < expected and row.status in {"SUCCESS", "REUSED"}:
        staleness = "STALE"
    return {"dataset": row.dataset, "status": row.status, "latest_source_date": row.latest_source_date, "attempt_latest_source_date": row.attempt_latest_source_date, "expected_latest_source_date": expected, "source_age_days": source_age_days, "last_attempt_at": row.last_attempt_at, "last_fetch_at": row.last_fetch_at, "last_http_success_at": row.last_http_success_at, "last_fully_successful_sync": row.last_fully_successful_sync, "last_usable_data_at": row.last_usable_data_at, "last_successful_sync": row.last_successful_sync, "records": row.records, "usable_records": row.usable_records, "stored_records": row.stored_records, "physical_requests_this_attempt": row.physical_requests_this_attempt, "rows_received_this_attempt": row.rows_received_this_attempt, "rows_accepted_this_attempt": row.rows_accepted_this_attempt, "rows_rejected_this_attempt": row.rows_rejected_this_attempt, "rows_versioned_this_attempt": row.rows_versioned_this_attempt, "observations_reused_this_attempt": row.observations_reused_this_attempt, "stored_rows_total": row.stored_rows_total, "counter_attempt_id": row.counter_attempt_id, "counter_semantics_version": row.counter_semantics_version, "counters_are_current_attempt": row.counters_are_current_attempt, "historical_pre_v5_counters": metadata.get("legacy_pre_v5_counter_snapshot"), "blocking_reason": row.last_error_code or metadata.get("blocking_reason"), "staleness": staleness, "metadata": metadata, "error_code": row.last_error_code}


def _provider_state(sync: list[DataSyncStatus]) -> dict[str, Any]:
    """Describe global provider coverage for observability.

    An absent or incomplete sync-status set is unknown, never available.  The
    source-level state remains visible, but it is not a universal veto for
    stocks whose own point-in-time inputs pass readiness.
    """
    by_dataset = {row.dataset: row for row in sync}
    expected_by_dataset = {dataset: authoritative_expected_latest_source_date(dataset) for dataset in CURRENT_SCORE_DATASETS}
    def blocked(status: str, reason_code: str, *, blocking_sources: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
        return {"status": status, "reason_code": reason_code, "provider": "FinMind", "score_policy": "FAIL_CLOSED", "numeric_scores_allowed": False, "score_ready": False, "score_blocked": True, "score_blocking_reason": "SCORE_BLOCKED_BY_SOURCE_COVERAGE", "blocking_sources": blocking_sources or [], **extra}

    if not sync:
        return blocked("DATA_INSUFFICIENT", "NO_AUTHORITATIVE_SYNC_STATUS")

    def failure_code(row: DataSyncStatus) -> str | None:
        if row.last_error_code in GLOBAL_PROVIDER_FAILURE_CODES:
            return row.last_error_code
        metadata = row.metadata_json or {}
        coverage = metadata.get("coverage") if isinstance(metadata, dict) else None
        candidate = coverage.get("fatal_code") if isinstance(coverage, dict) else None
        return candidate if candidate in GLOBAL_PROVIDER_FAILURE_CODES else None

    fatal = next((code for code in (failure_code(row) for row in sync) if code), None)
    if fatal:
        return blocked("QUOTA_EXHAUSTED" if fatal == "QUOTA_EXHAUSTED" else "PROVIDER_UNAVAILABLE", fatal, blocking_sources=[{"dataset": row.dataset, "reason_code": fatal} for row in sync if failure_code(row) == fatal])

    missing = [dataset for dataset in CURRENT_SCORE_DATASETS if dataset not in by_dataset]
    if missing:
        return blocked("DATA_INSUFFICIENT", "MISSING_REQUIRED_DATA_SYNC_STATUS", missing_datasets=missing)

    blockers: list[dict[str, Any]] = []
    for dataset in CURRENT_SCORE_DATASETS:
        row = by_dataset[dataset]
        expected = expected_by_dataset[dataset]
        metadata = row.metadata_json or {}
        coverage = metadata.get("coverage") if isinstance(metadata, dict) and isinstance(metadata.get("coverage"), dict) else (metadata if isinstance(metadata, dict) else {})
        if row.status == "WAITING_FOR_PROVIDER_PUBLICATION":
            blockers.append({"dataset": dataset, "reason_code": "WAITING_FOR_PROVIDER_PUBLICATION", "expected_source_date": expected, "actual_source_date": row.latest_source_date, "next_check_at": coverage.get("next_publication_check_at")})
            continue
        if row.status == "QUOTA_EXHAUSTED" or coverage.get("fatal_code") == "QUOTA_EXHAUSTED":
            blockers.append({"dataset": dataset, "reason_code": "QUOTA_EXHAUSTED"})
            continue
        if dataset == "TaiwanStockTradingDailyReport" and int(coverage.get("retryable_pending", 0) or 0) > 0:
            blockers.append({"dataset": dataset, "reason_code": "BROKER_RETRY_PENDING", "retryable_pending": int(coverage.get("retryable_pending", 0))})
        if dataset == HOLDING_DISTRIBUTION_DATASET and coverage.get("publication_state") == "HOLDING_PUBLICATION_PARTIAL":
            blockers.append({"dataset": dataset, "reason_code": "HOLDING_PUBLICATION_PARTIAL", "expected_source_date": expected, "actual_source_date": row.latest_source_date, "publication_probe": coverage.get("publication_probe")})
        if row.status not in {"SUCCESS", "REUSED"}:
            blockers.append({"dataset": dataset, "reason_code": row.last_error_code or "INCOMPLETE_PROVIDER_COVERAGE", "status": row.status})
            continue
        if expected is None or row.latest_source_date is None:
            blockers.append({"dataset": dataset, "reason_code": "NO_AUTHORITATIVE_CURRENT_SOURCE_DATE", "expected_source_date": expected, "actual_source_date": row.latest_source_date})
        elif row.staleness_state != "FRESH" or row.latest_source_date < expected:
            blockers.append({"dataset": dataset, "reason_code": "STALE_PROVIDER_COVERAGE", "expected_source_date": expected, "actual_source_date": row.latest_source_date, "staleness": row.staleness_state})
        if dataset == HOLDING_DISTRIBUTION_DATASET:
            holding_schema = coverage.get("holding_schema")
            if not isinstance(holding_schema, dict):
                blockers.append({"dataset": dataset, "reason_code": "HOLDING_COVERAGE_NOT_VERIFIED"})
            elif holding_schema.get("complete") is not True:
                blockers.append({"dataset": dataset, "reason_code": "HOLDING_BUCKETS_INCOMPLETE", **holding_schema})
    if blockers:
        reason = next((item["reason_code"] for item in blockers if item["reason_code"] in {"WAITING_FOR_PROVIDER_PUBLICATION", "HOLDING_PUBLICATION_PARTIAL", "QUOTA_EXHAUSTED", "BROKER_RETRY_PENDING", "HOLDING_BUCKETS_INCOMPLETE"}), blockers[0]["reason_code"])
        status = "WAITING_FOR_PROVIDER_PUBLICATION" if reason == "WAITING_FOR_PROVIDER_PUBLICATION" else ("QUOTA_EXHAUSTED" if reason == "QUOTA_EXHAUSTED" else "PARTIAL")
        return blocked(status, reason, blocking_sources=blockers)
    return {"status": "AVAILABLE", "reason_code": None, "provider": "FinMind", "score_policy": SCORE_VERSION.upper().replace("-", "_"), "numeric_scores_allowed": True, "score_ready": False, "score_blocked": False, "score_blocking_reason": None, "blocking_sources": []}
