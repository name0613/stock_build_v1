"""Run a rollback-clean point-in-time reconstruction against the migrated DB."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from backend.app.calendar import expected_trading_sessions
from backend.app.db import engine
from backend.app.ingestion import calculate_stock_features_and_score, ingest_records
from backend.app.models import Base, BrokerDaily, ForeignShareholdingDaily, HoldingDistribution, InstitutionalDaily, PriceDaily, SourceRevision, Stock


def run() -> dict[str, object]:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("PIT evidence must run against PostgreSQL, not local SQLite")
    schema = f"pit_evidence_{uuid.uuid4().hex[:12]}"
    end = date(2026, 8, 20)
    sessions = expected_trading_sessions(end, 21)
    t1 = datetime(2026, 8, 20, 13, tzinfo=timezone.utc)
    before = t1 - timedelta(hours=1)
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {schema}"))
        mapped = engine.connect().execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(mapped)
        db = Session(bind=mapped)
        db.add(Stock(stock_id="2330", stock_name="PIT fixture", market="上市", security_type="股票", is_common_stock=True))
        db.commit()
        for day in sessions:
            ingest_records(db, "TaiwanStockInstitutionalInvestorsBuySellWide", [{"stock_id": "2330", "date": day, "Foreign_Investor_buy": 110, "Foreign_Investor_sell": 10, "Foreign_Dealer_Self_buy": 20, "Foreign_Dealer_Self_sell": 10, "Investment_Trust_Buy": 30, "Investment_Trust_Sell": 10, "Dealer_Buy": 15, "Dealer_Sell": 10, "Dealer_self_Buy": 5, "Dealer_self_Sell": 1, "Dealer_Hedging_Buy": 4, "Dealer_Hedging_Sell": 2}])
            ingest_records(db, "TaiwanStockShareholding", [{"stock_id": "2330", "date": day, "ForeignInvestmentShares": 1000 + day.day, "ForeignInvestmentSharesRatio": 40 + day.day / 100}])
            ingest_records(db, "TaiwanStockPrice", [{"stock_id": "2330", "date": day, "close": 100 + day.day, "TradingVolume": 100000}])
            ingest_records(db, "TaiwanStockTradingDailyReport", [{"stock_id": "2330", "date": day, "securities_trader_id": "PIT", "buy_volume": 100, "sell_volume": 10}])
        for day in (end - timedelta(days=28), end - timedelta(days=21), end - timedelta(days=14), end - timedelta(days=7), end):
            ingest_records(db, "TaiwanStockHoldingSharesPer", [{"stock_id": "2330", "date": day, "HoldingSharesLevel": "400,001-600,000", "percent": 10, "people": 3}])
        for model in (InstitutionalDaily, ForeignShareholdingDaily, PriceDaily, BrokerDaily, HoldingDistribution, SourceRevision):
            db.execute(update(model).values(fetched_at=before))
        db.commit()
        first = calculate_stock_features_and_score(db, "2330", end, t1)
        sealed = calculate_stock_features_and_score(db, "2330", end, t1)
        ingest_records(db, "TaiwanStockInstitutionalInvestorsBuySellWide", [{"stock_id": "2330", "date": sessions[-1], "Foreign_Investor_buy": 0, "Foreign_Investor_sell": 1000, "Foreign_Dealer_Self_buy": 20, "Foreign_Dealer_Self_sell": 10, "Investment_Trust_Buy": 30, "Investment_Trust_Sell": 10, "Dealer_Buy": 15, "Dealer_Sell": 10, "Dealer_self_Buy": 5, "Dealer_self_Sell": 1, "Dealer_Hedging_Buy": 4, "Dealer_Hedging_Sell": 2}])
        later = calculate_stock_features_and_score(db, "2330", end, datetime.now(timezone.utc) + timedelta(seconds=1))
        evidence = {"database_dialect": engine.dialect.name, "temporary_schema": schema, "t1_cutoff": t1.isoformat(), "first": {"score": first.score, "input_snapshot_hash": first.input_snapshot_hash, "formula_hash": first.formula_hash}, "reconstructed_at_t1": {"score": sealed.score, "input_snapshot_hash": sealed.input_snapshot_hash}, "later_cutoff": {"score": later.score, "input_snapshot_hash": later.input_snapshot_hash}, "historical_identical": first.score == sealed.score and first.input_snapshot_hash == sealed.input_snapshot_hash, "later_cutoff_distinct": later.input_snapshot_hash != first.input_snapshot_hash, "provenance_rows": db.query(SourceRevision).count()}
        db.close()
        mapped.close()
        return evidence
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))


if __name__ == "__main__":
    result = run()
    output = Path("deployment_evidence/PIT_POSTGRES_EVIDENCE.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(output), "historical_identical": result["historical_identical"], "later_cutoff_distinct": result["later_cutoff_distinct"], "secrets_included": False}, ensure_ascii=False))
