from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from backend.app.db import SessionLocal, init_db
from backend.app.ingestion import calculate_stock_features_and_score
from backend.app.models import Stock


if __name__ == "__main__":
    init_db()
    db = SessionLocal()
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(), "source_date": date(2026, 8, 20).isoformat(), "scores": 0, "statuses": {}, "secrets_included": False}
    try:
        for stock_id, in db.query(Stock.stock_id).filter(Stock.is_common_stock.is_(True)).all():
            score = calculate_stock_features_and_score(db, stock_id, date(2026, 8, 20))
            evidence["scores"] += 1
            evidence["statuses"][score.status] = evidence["statuses"].get(score.status, 0) + 1
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        Path("deployment_evidence").mkdir(exist_ok=True)
        Path("deployment_evidence/UNIVERSE_SCORE_EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        db.close()
    print(json.dumps({"path": "deployment_evidence/UNIVERSE_SCORE_EVIDENCE.json", "scores": evidence["scores"], "statuses": evidence["statuses"], "secrets_included": False}, ensure_ascii=False))

