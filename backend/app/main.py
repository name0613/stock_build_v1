from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import asc, desc, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db, init_db
from .ingestion import seed_score_version
from .models import AccumulationFeature, AccumulationScore, BrokerDaily, DataSyncStatus, ForeignShareholdingDaily, HoldingDistribution, InstitutionalDaily, JobRun, PriceDaily, Stock
from .schemas import PaginatedStocks, StockListItem

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
        return {"status": "ok", "service": "api", "database": "ok", "score_version": settings.score_version, "timezone": settings.timezone}
    except Exception:
        raise HTTPException(status_code=503, detail={"status": "degraded", "database": "unavailable"})


@app.get("/api/summary")
def summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    total = db.scalar(select(func.count()).select_from(Stock).where(Stock.is_common_stock.is_(True))) or 0
    counts = {status: db.scalar(select(func.count()).select_from(AccumulationScore).where(AccumulationScore.source_date == _latest_score_date(db), AccumulationScore.status == status)) or 0 for status in ("STRONG_ACCUMULATION", "ACCUMULATION", "WATCH", "DATA_INSUFFICIENT", "NO_STRONG_EVIDENCE")}
    sync = db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all()
    return {"stock_count": total, "strong_count": counts["STRONG_ACCUMULATION"], "accumulation_count": counts["ACCUMULATION"], "watch_count": counts["WATCH"], "data_insufficient_count": counts["DATA_INSUFFICIENT"], "no_strong_evidence_count": counts["NO_STRONG_EVIDENCE"], "latest_score_date": _latest_score_date(db), "last_data_update": max((s.last_successful_sync for s in sync if s.last_successful_sync), default=None), "sync_status": [{"dataset": s.dataset, "status": s.status, "latest_source_date": s.latest_source_date, "last_successful_sync": s.last_successful_sync, "records": s.records, "error_code": s.last_error_code} for s in sync]}


@app.get("/api/stocks", response_model=PaginatedStocks)
def stocks(
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), search: str | None = Query(None, max_length=64), market: str | None = Query(None), industry: str | None = Query(None), status: str | None = Query(None), min_score: float | None = Query(None, ge=0, le=100), sort: str = Query("score"), order: str = Query("desc"),
    db: Session = Depends(get_db),
) -> PaginatedStocks:
    latest = _latest_score_date(db)
    base = select(Stock).where(Stock.is_common_stock.is_(True))
    if search:
        needle = f"%{search.strip()}%"
        base = base.where((Stock.stock_id.ilike(needle)) | (Stock.stock_name.ilike(needle)))
    if market:
        base = base.where(Stock.market == market)
    if industry:
        base = base.where(Stock.industry == industry)
    allowed_sort = {"stock_id": Stock.stock_id, "stock_name": Stock.stock_name, "market": Stock.market, "industry": Stock.industry}
    sort_column = allowed_sort.get(sort, Stock.stock_id)
    score_subquery = select(AccumulationScore.score).where(AccumulationScore.stock_id == Stock.stock_id, AccumulationScore.source_date == latest, AccumulationScore.score_version == settings.score_version).order_by(AccumulationScore.calculated_at.desc()).limit(1).scalar_subquery() if latest else None
    if latest:
        if status:
            base = base.where(AccumulationScore.stock_id == Stock.stock_id, AccumulationScore.source_date == latest, AccumulationScore.status == status, AccumulationScore.score_version == settings.score_version)
        if min_score is not None:
            base = base.where(AccumulationScore.stock_id == Stock.stock_id, AccumulationScore.source_date == latest, AccumulationScore.score >= min_score, AccumulationScore.score_version == settings.score_version)
    if sort == "score" and score_subquery is not None:
        sort_column = score_subquery
    base = base.order_by((desc(sort_column) if order.lower() == "desc" else asc(sort_column)), asc(Stock.stock_id))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(base.offset((page - 1) * page_size).limit(page_size)).all()
    items = [_stock_item(db, stock, latest) for stock in rows]
    return PaginatedStocks(items=items, total=total, page=page, page_size=page_size)


@app.get("/api/rankings")
def rankings(kind: str = Query("top"), limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)) -> dict[str, Any]:
    latest = _latest_score_date(db)
    if latest is None:
        return {"source_date": None, "items": []}
    query = select(AccumulationScore, Stock).join(Stock, Stock.stock_id == AccumulationScore.stock_id).where(AccumulationScore.source_date == latest, AccumulationScore.score_version == settings.score_version, Stock.is_common_stock.is_(True), AccumulationScore.score.is_not(None)).order_by(desc(AccumulationScore.score)).limit(limit)
    rows = db.execute(query).all()
    return {"source_date": latest, "kind": kind, "items": [{"stock_id": s.stock_id, "stock_name": s.stock_name, "market": s.market, "score": sc.score, "status": sc.status, "components": sc.components} for sc, s in rows]}


@app.get("/api/stocks/{stock_id}")
def stock_detail(stock_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    stock = db.get(Stock, stock_id)
    if stock is None:
        raise HTTPException(status_code=404, detail="stock not found")
    latest = _latest_score_date(db)
    return {"stock": {"stock_id": stock.stock_id, "stock_name": stock.stock_name, "market": stock.market, "industry": stock.industry}, "score": _score_dict(db, stock_id, latest), "sources": _source_status(db, stock_id), "institutional": _rows(db, InstitutionalDaily, stock_id), "foreign_holding": _rows(db, ForeignShareholdingDaily, stock_id), "holding_distribution": _rows(db, HoldingDistribution, stock_id), "brokers": _broker_summary(db, stock_id), "prices": _rows(db, PriceDaily, stock_id), "score_history": _score_history(db, stock_id)}


@app.get("/api/stocks/{stock_id}/institutional")
def institutional(stock_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _rows(db, InstitutionalDaily, stock_id)


@app.get("/api/stocks/{stock_id}/foreign-holding")
def foreign_holding(stock_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _rows(db, ForeignShareholdingDaily, stock_id)


@app.get("/api/stocks/{stock_id}/holding-distribution")
def holding_distribution(stock_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _rows(db, HoldingDistribution, stock_id)


@app.get("/api/stocks/{stock_id}/brokers")
def brokers(stock_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _broker_summary(db, stock_id)


@app.get("/api/stocks/{stock_id}/score-history")
def score_history(stock_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return _score_history(db, stock_id)


@app.get("/api/data-status")
def data_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all()
    jobs = db.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(50)).all()
    return {"datasets": [{"dataset": row.dataset, "status": row.status, "latest_source_date": row.latest_source_date, "last_successful_sync": row.last_successful_sync, "records": row.records, "error_code": row.last_error_code} for row in rows], "jobs": [{"dataset": j.dataset, "status": j.status, "requested_date": j.requested_date, "started_at": j.started_at, "finished_at": j.finished_at, "records": j.records, "duration_ms": j.duration_ms, "retry_count": j.retry_count, "error": j.error} for j in jobs]}


def _latest_score_date(db: Session) -> date | None:
    return db.scalar(select(func.max(AccumulationScore.source_date)).where(AccumulationScore.score_version == settings.score_version))


def _stock_item(db: Session, stock: Stock, latest: date | None) -> StockListItem:
    score = db.scalar(select(AccumulationScore).where(AccumulationScore.stock_id == stock.stock_id, AccumulationScore.source_date == latest, AccumulationScore.score_version == settings.score_version)) if latest else None
    price = db.scalar(select(PriceDaily).where(PriceDaily.stock_id == stock.stock_id).order_by(PriceDaily.source_date.desc()).limit(1))
    feature = db.scalar(select(AccumulationFeature).where(AccumulationFeature.stock_id == stock.stock_id, AccumulationFeature.source_date == latest).limit(1)) if latest else None
    return StockListItem(stock_id=stock.stock_id, stock_name=stock.stock_name, market=stock.market, industry=stock.industry, price=price.close if price else None, price_change=price.change if price else None, score=score.score if score else None, status=score.status if score else "DATA_INSUFFICIENT", score_version=score.score_version if score else None, features=feature.values if feature else {}, coverage=score.coverage if score else {}, latest_data=feature.latest_source_date if feature else None)


def _score_dict(db: Session, stock_id: str, latest: date | None) -> dict[str, Any]:
    if latest is None:
        return {"score": None, "status": "DATA_INSUFFICIENT", "score_version": settings.score_version}
    score = db.scalar(select(AccumulationScore).where(AccumulationScore.stock_id == stock_id, AccumulationScore.source_date == latest, AccumulationScore.score_version == settings.score_version))
    if not score:
        return {"score": None, "status": "DATA_INSUFFICIENT", "score_version": settings.score_version}
    return {"score": score.score, "status": score.status, "score_version": score.score_version, "components": score.components, "explanation": score.explanation, "coverage": score.coverage, "source_date": score.source_date, "calculated_at": score.calculated_at}


def _rows(db: Session, model: type[Any], stock_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(select(model).where(model.stock_id == stock_id).order_by(model.source_date)).all()
    return [{key: getattr(row, key) for key in model.__table__.columns.keys() if key not in {"id", "stock_id"}} | {"stock_id": stock_id} for row in rows]


def _broker_summary(db: Session, stock_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(select(BrokerDaily).where(BrokerDaily.stock_id == stock_id, BrokerDaily.source_date >= func.current_date() - 30).order_by(BrokerDaily.source_date, BrokerDaily.securities_trader_id)).all()
    by_broker: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = by_broker.setdefault(row.securities_trader_id, {"securities_trader_id": row.securities_trader_id, "securities_trader_name": row.securities_trader_name, "buy_volume": 0, "sell_volume": 0, "net_volume": 0, "positive_days": 0, "negative_days": 0})
        item["buy_volume"] += row.buy_volume or 0
        item["sell_volume"] += row.sell_volume or 0
        item["net_volume"] += row.net_volume or 0
        item["positive_days"] += 1 if (row.net_volume or 0) > 0 else 0
        item["negative_days"] += 1 if (row.net_volume or 0) < 0 else 0
    return sorted(by_broker.values(), key=lambda item: item["net_volume"], reverse=True)[:20]


def _score_history(db: Session, stock_id: str) -> list[dict[str, Any]]:
    rows = db.scalars(select(AccumulationScore).where(AccumulationScore.stock_id == stock_id, AccumulationScore.score_version == settings.score_version).order_by(AccumulationScore.source_date)).all()
    return [{"source_date": row.source_date, "score": row.score, "status": row.status, "components": row.components} for row in rows]


def _source_status(db: Session, stock_id: str) -> dict[str, Any]:
    mapping = {"institutional": InstitutionalDaily, "foreign_holding": ForeignShareholdingDaily, "holding_distribution": HoldingDistribution, "broker": BrokerDaily, "price": PriceDaily}
    result = {}
    for name, model in mapping.items():
        result[name] = {"dataset": getattr(model, "__tablename__", name), "latest_source_date": db.scalar(select(func.max(model.source_date)).where(model.stock_id == stock_id)), "row_count": db.scalar(select(func.count()).select_from(model).where(model.stock_id == stock_id)) or 0}
    return result
