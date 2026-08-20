from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from backend.app.config import get_settings
from backend.app.db import SessionLocal, init_db
from backend.app.finmind import FinMindClient, FinMindError
from backend.app.ingestion import calculate_stock_features_and_score, ingest_records


def weekdays(start: date, end: date) -> list[str]:
    result = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def run(stock_ids: list[str], start_date: date, end_date: date, include_broker: bool) -> None:
    init_db()
    db = SessionLocal()
    client = FinMindClient(get_settings())
    datasets = ["TaiwanStockInstitutionalInvestorsBuySellWide", "TaiwanStockShareholding", "TaiwanStockHoldingSharesPer", "TaiwanStockPrice"]
    if include_broker:
        datasets.append("TaiwanStockTradingDailyReport")
    dates = weekdays(start_date, end_date)
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "stock_ids": stock_ids, "dates": dates, "datasets": datasets, "success": 0, "empty": 0, "failed": 0, "secrets_included": False}
    checkpoint_path = Path("data/raw/checkpoints/targeted-backfill-v2.json")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.exists() else {"completed": []}
    completed = set(checkpoint.get("completed", []))
    try:
        for stock_id in stock_ids:
            for requested_date in dates:
                for dataset in datasets:
                    key = f"{dataset}:{stock_id}:{requested_date}"
                    if key in completed:
                        continue
                    try:
                        records, _ = client.fetch(dataset, stock_id, requested_date, requested_date)
                        if records:
                            ingest_records(db, dataset, records)
                            evidence["success"] += 1
                        else:
                            evidence["empty"] += 1
                        checkpoint["completed"].append(key)
                    except FinMindError as exc:
                        evidence["failed"] += 1
                        checkpoint.setdefault("failed", []).append({"key": key, "code": exc.code})
                    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
                    time.sleep(0.12)
        for stock_id in stock_ids:
            score = calculate_stock_features_and_score(db, stock_id, end_date)
            evidence.setdefault("scores", {})[stock_id] = {"score": score.score, "status": score.status, "coverage": score.coverage}
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        Path("deployment_evidence").mkdir(exist_ok=True)
        Path("deployment_evidence/TARGETED_BACKFILL_EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        db.close()
    print(json.dumps({"path": "deployment_evidence/TARGETED_BACKFILL_EVIDENCE.json", "success": evidence["success"], "empty": evidence["empty"], "failed": evidence["failed"], "secrets_included": False}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default=(date.today() - timedelta(days=35)).isoformat())
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--stock-ids", nargs="+", default=["2330", "2317", "2454"])
    parser.add_argument("--include-broker", action="store_true")
    args = parser.parse_args()
    run(args.stock_ids, date.fromisoformat(args.start_date), date.fromisoformat(args.end_date), args.include_broker)
