from __future__ import annotations

import logging
import json
import re
from pathlib import Path
from datetime import date, datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import asc, case, desc, func, nulls_last, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db, init_db
from .finmind import GLOBAL_PROVIDER_FAILURE_CODES
from .features import holding_distribution_features
from .ingestion import authoritative_source_state_hash, score_snapshot_state, seed_score_version
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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

CURRENT_SCORE_DATASETS = (
    "TaiwanStockInfo",
    "TaiwanStockInstitutionalInvestorsBuySellWide",
    "TaiwanStockShareholding",
    "TaiwanStockHoldingSharesPer",
    "TaiwanStockTradingDailyReport",
    "TaiwanStockPrice",
)


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
    return {"stock_count": total, "strong_count": counts["STRONG_ACCUMULATION"], "accumulation_count": counts["ACCUMULATION"], "watch_count": counts["WATCH"], "data_insufficient_count": counts["DATA_INSUFFICIENT"], "no_strong_evidence_count": counts["NO_STRONG_EVIDENCE"], "status_invariant": sum(counts.values()) == total, "latest_score_date": current_latest, "historical_latest_score_date": historical_latest, "score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH, "last_data_update": max(last_updates, default=None), "provider_state": provider_state, "sync_status": [_sync_dict(s) for s in sync]}


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
    items = [_stock_item_from_row(row) for row in rows]
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


@app.get("/api/stocks/{stock_id}")
def stock_detail(stock_id: str, limit: int = Query(365, ge=1, le=1000), db: Session = Depends(get_db)) -> dict[str, Any]:
    stock = db.get(Stock, stock_id)
    if stock is None:
        raise HTTPException(status_code=404, detail="stock not found")
    sync = db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all()
    provider_state = _provider_state(sync)
    latest = _current_score_date(db, provider_state, sync)
    return {"stock": {"stock_id": stock.stock_id, "stock_name": stock.stock_name, "market": stock.market, "industry": stock.industry}, "score": _score_dict(db, stock_id, latest), "provider_state": provider_state, "sources": _source_status(db, stock_id), "institutional": _rows(db, InstitutionalDaily, stock_id, min(limit, 365), "TaiwanStockInstitutionalInvestorsBuySellWide"), "foreign_holding": _rows(db, ForeignShareholdingDaily, stock_id, min(limit, 365), "TaiwanStockShareholding"), "holding_distribution": _rows(db, HoldingDistribution, stock_id, min(limit, 200), "TaiwanStockHoldingSharesPer"), "holding_series": _holding_chart_series(db, stock_id, min(limit, 200)), "brokers": _broker_summary(db, stock_id), "prices": _rows(db, PriceDaily, stock_id, min(limit, 365), "TaiwanStockPrice"), "score_history": _score_history(db, stock_id, min(limit, 365)), "calendar_version": CALENDAR_VERSION}


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
    return {"datasets": [_sync_dict(row) for row in rows], "jobs": [{"dataset": j.dataset, "status": j.status, "requested_date": j.requested_date, "requested_start_date": j.requested_start_date, "requested_end_date": j.requested_end_date, "started_at": j.started_at, "finished_at": j.finished_at, "records": j.records, "duration_ms": j.duration_ms, "retry_count": j.retry_count, "stocks_attempted": j.stocks_attempted, "stocks_completed": j.stocks_completed, "stocks_failed": j.stocks_failed, "checkpoint_state": j.checkpoint_state, "error_code": j.error_code, "error": j.error} for j in jobs]}


def _latest_score_date(db: Session) -> date | None:
    return db.scalar(select(func.max(AccumulationScore.source_date)).where(AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None)))


def _current_score_date(db: Session, provider_state: dict[str, Any], sync: list[DataSyncStatus] | None = None) -> date | None:
    """Return only a current-date score bound to a successful score JobRun."""
    sync = sync if sync is not None else list(db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all())
    readiness = _current_score_readiness(db, provider_state, sync)
    source_gate = provider_state.get("numeric_scores_allowed") is True
    provider_state["source_coverage_numeric_scores_allowed"] = source_gate
    provider_state["numeric_scores_allowed"] = readiness["ready"]
    provider_state["score_readiness"] = readiness
    if not readiness["ready"]:
        if source_gate:
            provider_state["status"] = "DATA_INSUFFICIENT"
            provider_state["reason_code"] = readiness["reason_code"]
        return None
    return date.fromisoformat(str(readiness["target_date"]))


def _current_score_readiness(db: Session, provider_state: dict[str, Any], sync: list[DataSyncStatus]) -> dict[str, Any]:
    if provider_state.get("numeric_scores_allowed") is not True:
        return {"ready": False, "reason_code": provider_state.get("reason_code") or "SOURCE_COVERAGE_NOT_READY", "target_date": None}
    expected_dates = [row.expected_latest_source_date for row in sync if row.dataset in CURRENT_SCORE_DATASETS and row.expected_latest_source_date]
    if not expected_dates:
        return {"ready": False, "reason_code": "CURRENT_SCORE_TARGET_DATE_MISSING", "target_date": None}
    target = max(expected_dates)
    job = db.scalar(
        select(JobRun)
        .where(JobRun.dataset == "score", JobRun.requested_end_date == target, JobRun.status.in_(("SUCCESS", "REUSED")))
        .order_by(JobRun.finished_at.desc(), JobRun.id.desc())
        .limit(1)
    )
    if job is None:
        return {"ready": False, "reason_code": "CURRENT_SCORE_JOB_NOT_SUCCESSFUL", "target_date": target.isoformat()}
    checkpoint = job.checkpoint_state or {}
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
        "stock_count_match": int(checkpoint.get("stock_count", -1)) == stock_count == current_scores["score_rows_count"],
        "score_rows_formula_match": current_scores["formula_hashes_match"] is True,
        "score_rows_input_bound": current_scores["input_snapshots_bound"] is True,
    }
    ready = all(checks.values())
    return {"ready": ready, "reason_code": None if ready else "CURRENT_SCORE_RUN_BINDING_FAILED", "target_date": target.isoformat(), "job_run_id": job.id, "checks": checks, "source_state_hash": current_source_hash, **current_scores}


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


def _stock_item_from_row(row: Any) -> StockListItem:
    stock, score, status, score_version, coverage, price, price_change, features, latest_data = row
    return StockListItem(stock_id=stock.stock_id, stock_name=stock.stock_name, market=stock.market, industry=stock.industry, price=price, price_change=price_change, score=score, status=status or "DATA_INSUFFICIENT", score_version=score_version, features=_json_dict(features), coverage=_json_dict(coverage), latest_data=latest_data)


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
        expected = sync.expected_latest_source_date if sync else None
        stock_staleness = "NO_DATA" if latest is None else ("STALE" if expected and latest < expected else "FRESH")
        result[name] = {"provider": "FinMind", "dataset": dataset, "latest_source_date": latest, "fetched_at": fetched, "last_successful_fetch": sync.last_http_success_at if sync else None, "last_fully_successful_sync": sync.last_fully_successful_sync if sync else None, "last_usable_data_at": sync.last_usable_data_at if sync else None, "attempt_latest_source_date": sync.attempt_latest_source_date if sync else None, "expected_latest_source_date": expected, "source_age_days": (expected - latest).days if expected and latest else None, "row_count": db.scalar(select(func.count()).select_from(model).where(model.stock_id == stock_id, model.source_dataset == dataset)) or 0, "staleness": stock_staleness, "global_sync_staleness": sync.staleness_state if sync else "UNKNOWN", "fallback": "not_used"}
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
    return {"dataset": row.dataset, "status": row.status, "latest_source_date": row.latest_source_date, "attempt_latest_source_date": row.attempt_latest_source_date, "expected_latest_source_date": row.expected_latest_source_date, "source_age_days": row.source_age_days, "last_attempt_at": row.last_attempt_at, "last_fetch_at": row.last_fetch_at, "last_http_success_at": row.last_http_success_at, "last_fully_successful_sync": row.last_fully_successful_sync, "last_usable_data_at": row.last_usable_data_at, "last_successful_sync": row.last_successful_sync, "records": row.records, "usable_records": row.usable_records, "stored_records": row.stored_records, "rows_received_this_attempt": row.rows_received_this_attempt, "rows_accepted_this_attempt": row.rows_accepted_this_attempt, "rows_rejected_this_attempt": row.rows_rejected_this_attempt, "rows_versioned_this_attempt": row.rows_versioned_this_attempt, "observations_reused_this_attempt": row.observations_reused_this_attempt, "stored_rows_total": row.stored_rows_total, "staleness": row.staleness_state, "metadata": row.metadata_json, "error_code": row.last_error_code}


def _provider_state(sync: list[DataSyncStatus]) -> dict[str, Any]:
    """Authoritatively gate every current score surface.

    An absent or incomplete sync-status set is unknown, never available.  The
    latest stored score remains queryable through the explicit score-history
    endpoint, but it cannot become a current score without this gate.
    """
    by_dataset = {row.dataset: row for row in sync}
    if not sync:
        return {"status": "DATA_INSUFFICIENT", "reason_code": "NO_AUTHORITATIVE_SYNC_STATUS", "provider": "FinMind", "score_policy": "FAIL_CLOSED", "numeric_scores_allowed": False}

    def failure_code(row: DataSyncStatus) -> str | None:
        if row.last_error_code in GLOBAL_PROVIDER_FAILURE_CODES:
            return row.last_error_code
        metadata = row.metadata_json or {}
        coverage = metadata.get("coverage") if isinstance(metadata, dict) else None
        candidate = coverage.get("fatal_code") if isinstance(coverage, dict) else None
        return candidate if candidate in GLOBAL_PROVIDER_FAILURE_CODES else None

    fatal = next((code for code in (failure_code(row) for row in sync) if code), None)
    if fatal:
        return {"status": "PROVIDER_UNAVAILABLE", "reason_code": fatal, "provider": "FinMind", "score_policy": "FAIL_CLOSED", "numeric_scores_allowed": False}

    missing = [dataset for dataset in CURRENT_SCORE_DATASETS if dataset not in by_dataset]
    if missing:
        return {"status": "DATA_INSUFFICIENT", "reason_code": "MISSING_REQUIRED_DATA_SYNC_STATUS", "missing_datasets": missing, "provider": "FinMind", "score_policy": "FAIL_CLOSED", "numeric_scores_allowed": False}

    for dataset in CURRENT_SCORE_DATASETS:
        row = by_dataset[dataset]
        if row.status not in {"SUCCESS", "REUSED"}:
            return {"status": "DATA_INSUFFICIENT", "reason_code": "INCOMPLETE_PROVIDER_COVERAGE", "provider": "FinMind", "score_policy": "FAIL_CLOSED", "numeric_scores_allowed": False}
        if row.expected_latest_source_date is None or row.latest_source_date is None:
            return {"status": "DATA_INSUFFICIENT", "reason_code": "NO_AUTHORITATIVE_CURRENT_SOURCE_DATE", "provider": "FinMind", "score_policy": "FAIL_CLOSED", "numeric_scores_allowed": False}
        if row.staleness_state != "FRESH":
            return {"status": "DATA_INSUFFICIENT", "reason_code": "STALE_PROVIDER_COVERAGE", "provider": "FinMind", "score_policy": "FAIL_CLOSED", "numeric_scores_allowed": False}
    return {"status": "AVAILABLE", "reason_code": None, "provider": "FinMind", "score_policy": SCORE_VERSION.upper().replace("-", "_"), "numeric_scores_allowed": True}
