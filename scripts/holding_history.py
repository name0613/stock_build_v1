from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from backend.app.config import get_settings
from backend.app.db import SessionLocal, init_db
from backend.app.finmind import FinMindClient
from backend.app.ingestion import calculate_stock_features_and_score, ingest_records


if __name__ == "__main__":
    init_db()
    db = SessionLocal()
    client = FinMindClient(get_settings())
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "dataset": "TaiwanStockHoldingSharesPer", "start_date": "2024-08-20", "end_date": "2026-08-20", "stocks": {}, "secrets_included": False}
    try:
        for stock_id in ["2330", "2317", "2454"]:
            records, meta = client.fetch("TaiwanStockHoldingSharesPer", stock_id, "2024-08-20", "2026-08-20")
            count = ingest_records(db, "TaiwanStockHoldingSharesPer", records)
            score = calculate_stock_features_and_score(db, stock_id, date(2026, 8, 20))
            evidence["stocks"][stock_id] = {"records": count, "latest_source_date": meta.get("source_date"), "score": score.score, "status": score.status}
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        Path("deployment_evidence").mkdir(exist_ok=True)
        Path("deployment_evidence/HOLDING_HISTORY_EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        db.close()
    print(json.dumps({"path": "deployment_evidence/HOLDING_HISTORY_EVIDENCE.json", "secrets_included": False}, ensure_ascii=False))

