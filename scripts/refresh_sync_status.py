from __future__ import annotations

from datetime import datetime, timezone

from backend.app.db import SessionLocal, init_db
from backend.app.models import BrokerDaily, DataSyncStatus, ForeignShareholdingDaily, HoldingDistribution, InstitutionalDaily, PriceDaily, Stock
from sqlalchemy import func, select


if __name__ == "__main__":
    init_db()
    db = SessionLocal()
    mapping = {"TaiwanStockInfo": Stock, "TaiwanStockInstitutionalInvestorsBuySellWide": InstitutionalDaily, "TaiwanStockShareholding": ForeignShareholdingDaily, "TaiwanStockHoldingSharesPer": HoldingDistribution, "TaiwanStockTradingDailyReport": BrokerDaily, "TaiwanStockPrice": PriceDaily}
    for dataset, model in mapping.items():
        count_query = select(func.count()).select_from(model)
        if model is Stock:
            count_query = count_query.where(Stock.is_common_stock.is_(True))
        count = db.scalar(count_query) or 0
        latest = db.scalar(select(func.max(model.source_date))) if model is not Stock else db.scalar(select(func.max(model.source_date)))
        item = db.get(DataSyncStatus, dataset)
        if item is None:
            item = DataSyncStatus(dataset=dataset, status="PARTIAL", records=0)
            db.add(item)
        item.records = count
        item.latest_source_date = latest
        item.status = "SUCCESS" if count else "PARTIAL"
        item.last_successful_sync = datetime.now(timezone.utc) if count else item.last_successful_sync
        item.last_error_code = None if count else "EMPTY_DATA"
    db.commit()
    db.close()
    print("sync status refreshed without secrets")
