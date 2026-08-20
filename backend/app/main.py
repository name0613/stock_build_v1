from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import asc, desc, func, nulls_last, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db, init_db
from .ingestion import seed_score_version
from .models import AccumulationFeature, AccumulationScore, BrokerDaily, DataSyncStatus, ForeignShareholdingDaily, HoldingDistribution, InstitutionalDaily, JobRun, PriceDaily, Stock
from .schemas import PaginatedStocks, StockListItem
from .scoring import FORMULA_HASH, SCORE_VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", docs_url="/api/docs", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])


@app.on_event("startup")
def startup() -> None:
    init_db()
    with next(get_db()) as db:
        seed_score_version(db)


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        db.execute(select(func.count()).select_from(Stock)).scalar_one()
        return {"status": "ok", "service": "api", "database": "ok", "score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH, "timezone": settings.timezone}
    except Exception:
        raise HTTPException(status_code=503, detail={"status": "degraded", "database": "unavailable"})


@app.get("/api/summary")
def summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    total, status_map = _canonical_statuses(db)
    counts = {status: sum(1 for value in status_map.values() if value == status) for status in ("STRONG_ACCUMULATION", "ACCUMULATION", "WATCH", "DATA_INSUFFICIENT", "NO_STRONG_EVIDENCE")}
    sync = db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all()
    last_updates = [s.last_fetch_at or s.last_successful_sync for s in sync if s.last_fetch_at or s.last_successful_sync]
    return {"stock_count": total, "strong_count": counts["STRONG_ACCUMULATION"], "accumulation_count": counts["ACCUMULATION"], "watch_count": counts["WATCH"], "data_insufficient_count": counts["DATA_INSUFFICIENT"], "no_strong_evidence_count": counts["NO_STRONG_EVIDENCE"], "status_invariant": sum(counts.values()) == total, "latest_score_date": _latest_score_date(db), "score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH, "last_data_update": max(last_updates, default=None), "sync_status": [_sync_dict(s) for s in sync]}


@app.get("/api/stocks", response_model=PaginatedStocks)
def stocks(
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), search: str | None = Query(None, max_length=64), market: str | None = Query(None), industry: str | None = Query(None), status: str | None = Query(None), min_score: float | None = Query(None, ge=0, le=100), sort: str = Query("score"), order: str = Query("desc"),
    db: Session = Depends(get_db),
) -> PaginatedStocks:
    latest = _latest_score_date(db)
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
    latest = _latest_score_date(db)
    if latest is None:
        return {"source_date": None, "score_version": SCORE_VERSION, "items": []}
    score_value, score_status, _, score_components = _score_subqueries(latest, components=True)
    query = select(Stock, score_value, score_status, score_components).where(Stock.is_common_stock.is_(True), score_value.is_not(None)).order_by(desc(score_value), asc(Stock.stock_id)).limit(limit)
    rows = db.execute(query).all()
    return {"source_date": latest, "kind": kind, "score_version": SCORE_VERSION, "items": [{"stock_id": row[0].stock_id, "stock_name": row[0].stock_name, "market": row[0].market, "score": row[1], "status": row[2], "components": row[3]} for row in rows]}


@app.get("/api/stocks/{stock_id}")
def stock_detail(stock_id: str, limit: int = Query(365, ge=1, le=1000), db: Session = Depends(get_db)) -> dict[str, Any]:
    stock = db.get(Stock, stock_id)
    if stock is None:
        raise HTTPException(status_code=404, detail="stock not found")
    latest = _latest_score_date(db)
    return {"stock": {"stock_id": stock.stock_id, "stock_name": stock.stock_name, "market": stock.market, "industry": stock.industry}, "score": _score_dict(db, stock_id, latest), "sources": _source_status(db, stock_id), "institutional": _rows(db, InstitutionalDaily, stock_id, min(limit, 365)), "foreign_holding": _rows(db, ForeignShareholdingDaily, stock_id, min(limit, 365)), "holding_distribution": _rows(db, HoldingDistribution, stock_id, min(limit, 200)), "holding_series": _holding_chart_series(db, stock_id, min(limit, 200)), "brokers": _broker_summary(db, stock_id), "prices": _rows(db, PriceDaily, stock_id, min(limit, 365)), "score_history": _score_history(db, stock_id, min(limit, 365))}


@app.get("/api/stocks/{stock_id}/institutional")
def institutional(stock_id: str, limit: int = Query(365, ge=1, le=1000), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _rows(db, InstitutionalDaily, stock_id, limit)


@app.get("/api/stocks/{stock_id}/foreign-holding")
def foreign_holding(stock_id: str, limit: int = Query(365, ge=1, le=1000), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _rows(db, ForeignShareholdingDaily, stock_id, limit)


@app.get("/api/stocks/{stock_id}/holding-distribution")
def holding_distribution(stock_id: str, limit: int = Query(200, ge=1, le=500), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _rows(db, HoldingDistribution, stock_id, limit)


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
    return db.scalar(select(func.max(AccumulationScore.source_date)).where(AccumulationScore.score_version == SCORE_VERSION))


def _canonical_statuses(db: Session) -> tuple[int, dict[str, str]]:
    stock_ids = list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True))).all())
    latest = _latest_score_date(db)
    if latest is None:
        return len(stock_ids), {stock_id: "DATA_INSUFFICIENT" for stock_id in stock_ids}
    scores = db.scalars(select(AccumulationScore).where(AccumulationScore.source_date == latest, AccumulationScore.score_version == SCORE_VERSION).order_by(AccumulationScore.calculated_at.desc())).all()
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
        return select(getattr(AccumulationScore, name)).where(AccumulationScore.stock_id == Stock.stock_id, AccumulationScore.source_date == latest, AccumulationScore.score_version == SCORE_VERSION).order_by(AccumulationScore.calculated_at.desc()).limit(1).correlate(Stock).scalar_subquery()
    return field("score"), field("status"), field("score_version"), field("components" if components else "coverage")


def _price_subqueries():
    latest_date = select(func.max(PriceDaily.source_date)).where(PriceDaily.stock_id == Stock.stock_id).correlate(Stock).scalar_subquery()
    return (select(PriceDaily.close).where(PriceDaily.stock_id == Stock.stock_id, PriceDaily.source_date == latest_date).limit(1).correlate(Stock).scalar_subquery(), select(PriceDaily.change).where(PriceDaily.stock_id == Stock.stock_id, PriceDaily.source_date == latest_date).limit(1).correlate(Stock).scalar_subquery())


def _feature_subqueries(latest: date | None):
    if latest is None:
        return select(func.json_object()).scalar_subquery(), select(func.cast(None, AccumulationFeature.latest_source_date.type)).scalar_subquery()
    return (select(AccumulationFeature.values).where(AccumulationFeature.stock_id == Stock.stock_id, AccumulationFeature.source_date == latest).order_by(AccumulationFeature.calculated_at.desc()).limit(1).correlate(Stock).scalar_subquery(), select(AccumulationFeature.latest_source_date).where(AccumulationFeature.stock_id == Stock.stock_id, AccumulationFeature.source_date == latest).order_by(AccumulationFeature.calculated_at.desc()).limit(1).correlate(Stock).scalar_subquery())


def _stock_item_from_row(row: Any) -> StockListItem:
    stock, score, status, score_version, coverage, price, price_change, features, latest_data = row
    return StockListItem(stock_id=stock.stock_id, stock_name=stock.stock_name, market=stock.market, industry=stock.industry, price=price, price_change=price_change, score=score, status=status or "DATA_INSUFFICIENT", score_version=score_version, features=features or {}, coverage=coverage or {}, latest_data=latest_data)


def _score_dict(db: Session, stock_id: str, latest: date | None) -> dict[str, Any]:
    if latest is None:
        return {"score": None, "status": "DATA_INSUFFICIENT", "score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH}
    score = db.scalar(select(AccumulationScore).where(AccumulationScore.stock_id == stock_id, AccumulationScore.source_date == latest, AccumulationScore.score_version == SCORE_VERSION).order_by(AccumulationScore.calculated_at.desc()).limit(1))
    if not score:
        return {"score": None, "status": "DATA_INSUFFICIENT", "score_version": SCORE_VERSION, "formula_hash": FORMULA_HASH}
    return {"score": score.score, "status": score.status, "score_version": score.score_version, "formula_hash": score.formula_hash or FORMULA_HASH, "components": score.components, "explanation": score.explanation, "coverage": score.coverage, "source_date": score.source_date, "calculated_at": score.calculated_at, "knowledge_cutoff": score.knowledge_cutoff, "input_snapshot_hash": score.input_snapshot_hash, "input_source_hashes": score.input_source_hashes}


def _rows(db: Session, model: type[Any], stock_id: str, limit: int) -> list[dict[str, Any]]:
    rows = db.scalars(select(model).where(model.stock_id == stock_id).order_by(model.source_date.desc()).limit(limit)).all()
    return [{key: getattr(row, key) for key in model.__table__.columns.keys() if key not in {"id", "stock_id"}} | {"stock_id": stock_id} for row in reversed(rows)]


def _broker_summary(db: Session, stock_id: str) -> list[dict[str, Any]]:
    latest_dates = db.scalars(
        select(BrokerDaily.source_date)
        .where(BrokerDaily.stock_id == stock_id)
        .distinct()
        .order_by(BrokerDaily.source_date.desc())
        .limit(20)
    ).all()
    rows = db.scalars(
        select(BrokerDaily).where(BrokerDaily.stock_id == stock_id, BrokerDaily.source_date.in_(latest_dates))
    ).all()
    by_broker: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = by_broker.setdefault(row.securities_trader_id, {"securities_trader_id": row.securities_trader_id, "securities_trader_name": row.securities_trader_name, "buy_volume": None, "sell_volume": None, "net_volume": None, "positive_days": 0, "negative_days": 0, "missing_days": 0})
        item["buy_volume"] = _nullable_add(item["buy_volume"], row.buy_volume)
        item["sell_volume"] = _nullable_add(item["sell_volume"], row.sell_volume)
        item["net_volume"] = _nullable_add(item["net_volume"], row.net_volume)
        if row.net_volume is None:
            item["missing_days"] += 1
        elif row.net_volume > 0:
            item["positive_days"] += 1
        elif row.net_volume < 0:
            item["negative_days"] += 1
    return sorted(by_broker.values(), key=lambda item: item["net_volume"] if item["net_volume"] is not None else float("-inf"), reverse=True)[:20]


def _nullable_add(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left + right


def _score_history(db: Session, stock_id: str, limit: int) -> list[dict[str, Any]]:
    rows = db.scalars(select(AccumulationScore).where(AccumulationScore.stock_id == stock_id, AccumulationScore.score_version == SCORE_VERSION).order_by(AccumulationScore.source_date.desc()).limit(limit)).all()
    return [{"source_date": row.source_date, "score": row.score, "status": row.status, "components": row.components, "formula_hash": row.formula_hash, "input_snapshot_hash": row.input_snapshot_hash} for row in reversed(rows)]


def _source_status(db: Session, stock_id: str) -> dict[str, Any]:
    mapping = {"institutional": (InstitutionalDaily, "TaiwanStockInstitutionalInvestorsBuySellWide"), "foreign_holding": (ForeignShareholdingDaily, "TaiwanStockShareholding"), "holding_distribution": (HoldingDistribution, "TaiwanStockHoldingSharesPer"), "broker": (BrokerDaily, "TaiwanStockTradingDailyReport"), "price": (PriceDaily, "TaiwanStockPrice")}
    result: dict[str, Any] = {}
    for name, (model, dataset) in mapping.items():
        latest = db.scalar(select(func.max(model.source_date)).where(model.stock_id == stock_id))
        fetched = db.scalar(select(func.max(model.fetched_at)).where(model.stock_id == stock_id))
        sync = db.get(DataSyncStatus, dataset)
        result[name] = {"provider": "FinMind", "dataset": dataset, "latest_source_date": latest, "fetched_at": fetched, "last_successful_fetch": sync.last_fetch_at if sync else None, "row_count": db.scalar(select(func.count()).select_from(model).where(model.stock_id == stock_id)) or 0, "staleness": sync.staleness_state if sync else "UNKNOWN", "fallback": "not_used"}
    result["major_shareholder_5pct"] = {"provider": "TWSE/TPEx/MOPS", "dataset": None, "status": "UNAVAILABLE_NOT_CONFIGURED", "fallback": "none"}
    return result


def _holding_chart_series(db: Session, stock_id: str, limit: int) -> dict[str, list[dict[str, Any]]]:
    rows = db.scalars(select(HoldingDistribution).where(HoldingDistribution.stock_id == stock_id).order_by(HoldingDistribution.source_date.desc()).limit(limit)).all()
    by_date: dict[date, dict[str, float | None]] = {}
    for row in rows:
        day = by_date.setdefault(row.source_date, {"400": None, "1000": None})
        for label, threshold in (("400", 400_000), ("1000", 1_000_000)):
            if row.holding_shares_threshold is not None and row.holding_shares_threshold >= threshold:
                if row.percent is None:
                    day[label] = None
                elif day[label] is None:
                    day[label] = row.percent
                else:
                    day[label] += row.percent
    dates = sorted(by_date)
    return {label: [{"source_date": day, "value": by_date[day][label]} for day in dates] for label in ("400", "1000")}


def _sync_dict(row: DataSyncStatus) -> dict[str, Any]:
    return {"dataset": row.dataset, "status": row.status, "latest_source_date": row.latest_source_date, "last_attempt_at": row.last_attempt_at, "last_fetch_at": row.last_fetch_at, "last_successful_sync": row.last_successful_sync, "records": row.records, "usable_records": row.usable_records, "stored_records": row.stored_records, "staleness": row.staleness_state, "error_code": row.last_error_code}
