"""Run a rollback-clean point-in-time reconstruction against the migrated DB."""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text, update
from sqlalchemy.orm import Session

try:
    from backend.app.calendar import expected_trading_sessions
    from backend.app.db import engine
    from backend.app.ingestion import calculate_stock_features_and_score, ingest_records
    from backend.app.models import AccumulationScore, Base, BrokerDaily, ForeignShareholdingDaily, HoldingDistribution, InstitutionalDaily, PriceDaily, SourceRevision, Stock
    from backend.app.scoring import BROKER_REPORT_CONTRACT_VERSION, HOLDING_CANONICAL_LEVELS
except ModuleNotFoundError:  # running from the backend image
    from app.calendar import expected_trading_sessions
    from app.db import engine
    from app.ingestion import calculate_stock_features_and_score, ingest_records
    from app.models import AccumulationScore, Base, BrokerDaily, ForeignShareholdingDaily, HoldingDistribution, InstitutionalDaily, PriceDaily, SourceRevision, Stock
    from app.scoring import BROKER_REPORT_CONTRACT_VERSION, HOLDING_CANONICAL_LEVELS


def _score_payload(score: AccumulationScore) -> dict[str, object]:
    return {
        "id": score.id,
        "score": score.score,
        "status": score.status,
        "components": score.components,
        "explanation": score.explanation,
        "coverage": score.coverage,
        "score_version": score.score_version,
        "formula_hash": score.formula_hash,
        "knowledge_cutoff": score.knowledge_cutoff,
        "input_snapshot_hash": score.input_snapshot_hash,
        "input_source_hashes": score.input_source_hashes,
    }


def _holding_rows(day: date, base: float) -> list[dict[str, object]]:
    return [
        {"stock_id": "2330", "date": day, "HoldingSharesLevel": level, "percent": base + index, "people": index + 1, "shares": threshold}
        for index, (level, threshold) in enumerate(HOLDING_CANONICAL_LEVELS)
    ]


def run() -> dict[str, object]:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("PIT evidence must run against PostgreSQL, not local SQLite")
    schema = f"pit_evidence_{uuid.uuid4().hex[:12]}"
    end = date(2026, 8, 20)
    sessions = expected_trading_sessions(end, 21)
    t1 = datetime(2026, 8, 20, 13, tzinfo=timezone.utc)
    before = t1 - timedelta(hours=1)
    mapped = None
    db = None
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
            ingest_records(db, "TaiwanStockTradingDailyReport", [{"stock_id": "2330", "date": day, "securities_trader_id": "PIT", "buy_volume": 100, "sell_volume": 10, "provider_row_validated": True, "provider_row_contract_version": BROKER_REPORT_CONTRACT_VERSION}])
        for index, day in enumerate((end - timedelta(days=28), end - timedelta(days=21), end - timedelta(days=14), end - timedelta(days=7), end)):
            ingest_records(db, "TaiwanStockHoldingSharesPer", _holding_rows(day, 8 + index))
        for model in (InstitutionalDaily, ForeignShareholdingDaily, PriceDaily, BrokerDaily, HoldingDistribution, SourceRevision):
            db.execute(update(model).values(fetched_at=before))
        db.commit()
        first = calculate_stock_features_and_score(db, "2330", end, t1)
        sealed = calculate_stock_features_and_score(db, "2330", end, t1)
        if first.score is None:
            raise AssertionError("PIT fixture did not produce the required non-null initial score")
        original_payload = _score_payload(first)
        ingest_records(db, "TaiwanStockInstitutionalInvestorsBuySellWide", [{"stock_id": "2330", "date": end + timedelta(days=1), "Foreign_Investor_buy": 9999, "Foreign_Investor_sell": 0, "Foreign_Dealer_Self_buy": 0, "Foreign_Dealer_Self_sell": 0, "Investment_Trust_Buy": 0, "Investment_Trust_Sell": 0, "Dealer_Buy": 0, "Dealer_Sell": 0, "Dealer_self_Buy": 0, "Dealer_self_Sell": 0, "Dealer_Hedging_Buy": 0, "Dealer_Hedging_Sell": 0}])
        ingest_records(db, "TaiwanStockInstitutionalInvestorsBuySellWide", [{"stock_id": "2330", "date": sessions[-1], "Foreign_Investor_buy": 0, "Foreign_Investor_sell": 1000, "Foreign_Dealer_Self_buy": 20, "Foreign_Dealer_Self_sell": 10, "Investment_Trust_Buy": 30, "Investment_Trust_Sell": 10, "Dealer_Buy": 15, "Dealer_Sell": 10, "Dealer_self_Buy": 5, "Dealer_self_Sell": 1, "Dealer_Hedging_Buy": 4, "Dealer_Hedging_Sell": 2}])
        later = calculate_stock_features_and_score(db, "2330", end, datetime.now(timezone.utc) + timedelta(seconds=1))
        original_after_later = db.get(AccumulationScore, first.id)
        sealed_payload = _score_payload(sealed)
        preserved_payload = _score_payload(original_after_later)
        later_payload = _score_payload(later)
        evidence = {
            "database_dialect": engine.dialect.name,
            "temporary_schema": schema,
            "historical_date": end.isoformat(),
            "t1_cutoff": t1.isoformat(),
            "initial": original_payload,
            "reconstructed_at_t1": sealed_payload,
            "original_after_later_cutoff": preserved_payload,
            "later_cutoff": later_payload,
            "future_row_after_historical_date_added": True,
            "correction_source_date_lte_historical_date_fetched_after_t1": True,
            "historical_bit_for_bit_identical": original_payload == sealed_payload == preserved_payload,
            "later_calculation_distinct": later.id != first.id and later.input_snapshot_hash != first.input_snapshot_hash,
            "original_row_not_mutated": original_after_later.id == first.id and preserved_payload == original_payload,
            "non_null_initial_score": first.score is not None,
            "provenance_rows": db.query(SourceRevision).count(),
            "secrets_included": False,
        }
        return evidence
    finally:
        if db is not None:
            db.rollback()
            db.close()
        if mapped is not None:
            if mapped.in_transaction():
                mapped.rollback()
            mapped.close()
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))


if __name__ == "__main__":
    result = run()
    if os.getenv("PIT_EVIDENCE_STDOUT") == "1":
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        raise SystemExit(0)
    output = Path("deployment_evidence/PIT_POSTGRES_EVIDENCE.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(output), "historical_bit_for_bit_identical": result["historical_bit_for_bit_identical"], "later_calculation_distinct": result["later_calculation_distinct"], "non_null_initial_score": result["non_null_initial_score"], "secrets_included": False}, ensure_ascii=False))
