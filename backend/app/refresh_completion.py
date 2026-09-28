"""Local-only completion checks for closed-market daily refreshes."""
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AccumulationScore, BrokerDaily, ForeignShareholdingDaily, HoldingDistribution, InstitutionalDaily, PriceDaily, Stock, StockRefreshIssue
from .refresh_policy import exclusion_predicate
from .scoring import FORMULA_HASH, SCORE_VERSION


def daily_refresh_completion(db: Session, target: date, stock_ids: list[str] | None = None) -> dict:
    # A numeric current-version score certifies the full rolling readiness
    # contract. Require the latest evaluation, exact target date and source
    # timestamps no newer than its cutoff; old fallback scores never qualify.
    universe = select(Stock.stock_id).where(Stock.is_common_stock.is_(True))
    if stock_ids is not None:
        universe = universe.where(Stock.stock_id.in_(stock_ids))
    all_ids = set(db.scalars(universe).all())
    skipped = set(db.scalars(select(StockRefreshIssue.stock_id).where(exclusion_predicate())).all()) & all_ids
    eligible = all_ids - skipped
    ranked = select(
        AccumulationScore.stock_id, AccumulationScore.score, AccumulationScore.status,
        AccumulationScore.knowledge_cutoff, AccumulationScore.formula_hash,
        AccumulationScore.input_snapshot_hash,
        func.row_number().over(partition_by=AccumulationScore.stock_id, order_by=(AccumulationScore.calculated_at.desc(), AccumulationScore.id.desc())).label("rank"),
    ).where(AccumulationScore.source_date == target, AccumulationScore.score_version == SCORE_VERSION)
    if stock_ids is not None:
        ranked = ranked.where(AccumulationScore.stock_id.in_(stock_ids))
    ranked = ranked.subquery()
    candidates = {
        row.stock_id: row.knowledge_cutoff
        for row in db.execute(select(ranked).where(ranked.c.rank == 1)).all()
        if row.stock_id in eligible and row.score is not None and row.status != "DATA_INSUFFICIENT"
        and row.knowledge_cutoff is not None and row.formula_hash == FORMULA_HASH and row.input_snapshot_hash
    }
    specs = (
        (InstitutionalDaily, "TaiwanStockInstitutionalInvestorsBuySellWide", target),
        (ForeignShareholdingDaily, "TaiwanStockShareholding", target),
        (PriceDaily, "TaiwanStockPrice", target),
        (BrokerDaily, "TaiwanStockTradingDailyReport", target),
        (HoldingDistribution, "TaiwanStockHoldingSharesPer", target - timedelta(days=(target.weekday() - 4) % 7)),
    )
    for model, dataset, expected in specs:
        if not candidates:
            break
        rows = db.execute(select(model.stock_id, func.max(model.source_date), func.max(model.fetched_at)).where(
            model.source_dataset == dataset, model.source_date <= expected, model.stock_id.in_(list(candidates)),
        ).group_by(model.stock_id)).all()
        candidates = {sid: candidates[sid] for sid, latest, fetched in rows if latest == expected and fetched is not None and fetched <= candidates[sid]}
    complete = set(candidates)
    pending = eligible - complete
    return {
        "target_date": target.isoformat(), "universe_count": len(all_ids),
        "eligible_count": len(eligible), "excluded_count": len(skipped),
        "completed_count": len(complete), "pending_count": len(pending),
        "all_complete": bool(all_ids) and not pending,
        "pending_stock_ids": sorted(pending), "completed_stock_ids": sorted(complete),
    }


def completion_summary(state: dict) -> dict:
    return {key: value for key, value in state.items() if not key.endswith("_stock_ids")}
