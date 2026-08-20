from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from backend.app.config import get_settings
from backend.app.db import SessionLocal, init_db
from backend.app.finmind import FinMindClient, FinMindError
from backend.app.ingestion import calculate_stock_features_and_score, ingest_records, sync_universe
from backend.app.models import DataSyncStatus, Stock


def mark(db, dataset: str, status: str, records: int, latest: str | None, code: str | None = None) -> None:
    item = db.get(DataSyncStatus, dataset)
    if item is None:
        item = DataSyncStatus(dataset=dataset, status=status, records=records)
        db.add(item)
    item.status = status
    item.records = records
    item.latest_source_date = date.fromisoformat(latest) if latest else None
    item.last_successful_sync = datetime.now(timezone.utc) if status == "SUCCESS" else item.last_successful_sync
    item.last_error_code = code
    item.last_error = None
    db.commit()


def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    init_db()
    db = SessionLocal()
    client = FinMindClient(settings)
    evidence: dict[str, object] = {"started_at": datetime.now(timezone.utc).isoformat(), "start_date": args.start_date, "end_date": args.end_date, "datasets": {}, "secrets_included": False}
    try:
        universe_count = sync_universe(db, client)
        evidence["datasets"]["TaiwanStockInfo"] = {"status": "SUCCESS", "records": universe_count}
        dataset_names = ["TaiwanStockInstitutionalInvestorsBuySellWide", "TaiwanStockShareholding", "TaiwanStockHoldingSharesPer", "TaiwanStockPrice"]
        for dataset in dataset_names:
            try:
                records, meta = client.fetch(dataset, start_date=args.start_date, end_date=args.end_date)
                count = ingest_records(db, dataset, records)
                mark(db, dataset, "SUCCESS", count, meta.get("source_date"))
                evidence["datasets"][dataset] = {"status": "SUCCESS", "records": count, "latest_source_date": meta.get("source_date"), "raw_evidence": meta.get("evidence", {}).get("paths", [])}
            except FinMindError as exc:
                mark(db, dataset, "FAILED", 0, None, exc.code)
                evidence["datasets"][dataset] = {"status": "FAILED", "records": 0, "error_code": exc.code}
        for stock_id in args.stock_ids:
            try:
                score = calculate_stock_features_and_score(db, stock_id, date.fromisoformat(args.end_date))
                evidence.setdefault("score_samples", {})[stock_id] = {"score": score.score, "status": score.status}
            except Exception as exc:
                evidence.setdefault("score_samples", {})[stock_id] = {"status": "FAILED", "error": type(exc).__name__}
        if args.broker:
            stock_ids = [row[0] for row in db.query(Stock.stock_id).filter(Stock.is_common_stock.is_(True)).all()]
            import asyncio
            evidence["broker_metrics"] = asyncio.run(client.fetch_broker_stocks(stock_ids, args.broker_start_date, args.end_date))
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        Path("deployment_evidence").mkdir(exist_ok=True)
        Path("deployment_evidence/LIVE_BOOTSTRAP_EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        db.close()
    print(json.dumps({"path": "deployment_evidence/LIVE_BOOTSTRAP_EVIDENCE.json", "secrets_included": False, "universe": evidence["datasets"].get("TaiwanStockInfo")}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default=(date.today() - timedelta(days=730)).isoformat())
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--broker-start-date", default=(date.today() - timedelta(days=90)).isoformat())
    parser.add_argument("--stock-ids", nargs="+", default=["2330", "2317", "2454"])
    parser.add_argument("--broker", action="store_true")
    run(parser.parse_args())
