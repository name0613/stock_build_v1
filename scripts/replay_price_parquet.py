"""Replay persisted formal TaiwanStockPrice Parquet into production tables.

This is an idempotent raw-replay utility, not a research-cache importer. It
uses the normal ingestion/upsert and SourceRevision paths and preserves the
sanitized raw fetch timestamp when present.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow.parquet as parquet
from sqlalchemy import select

from backend.app.db import SessionLocal, init_db
from backend.app.ingestion import calculate_stock_features_and_score, ingest_records
from backend.app.models import Stock


def replay(raw_root: Path, score_date: date | None = None) -> dict[str, object]:
    init_db()
    files = sorted((raw_root / "TaiwanStockPrice").glob("date=*/part-*.parquet"))
    if not files:
        raise SystemExit("No TaiwanStockPrice Parquet files found")
    rows = 0
    accepted = 0
    files_with_formal_money = 0
    raw_fetched_at: list[datetime] = []
    with SessionLocal() as db:
        for path in files:
            batch = parquet.read_table(path).to_pylist()
            rows += len(batch)
            if any(row.get("Trading_money") is not None for row in batch):
                files_with_formal_money += 1
            for row in batch:
                value = row.get("_evidence_fetched_at") or row.get("fetched_at")
                if value:
                    try:
                        raw_fetched_at.append(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
                    except ValueError:
                        continue
            accepted += ingest_records(db, "TaiwanStockPrice", batch)
        target = score_date or max((date.fromisoformat(str(row["date"])[:10]) for path in files for row in parquet.read_table(path, columns=["date"]).to_pylist()), default=date.today())
        # Pin the scoring snapshot to the sanitized raw fetch watermark.  A
        # replay can therefore be interrupted and rerun without creating a
        # new point-in-time score row merely because wall-clock time moved.
        knowledge_cutoff = max(raw_fetched_at, default=datetime.now(timezone.utc))
        stock_ids = list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True)).order_by(Stock.stock_id)).all())
        scored = 0
        for stock_id in stock_ids:
            calculate_stock_features_and_score(db, stock_id, target, knowledge_cutoff=knowledge_cutoff)
            scored += 1
    return {"started_at": datetime.now(timezone.utc).isoformat(), "raw_root": str(raw_root), "files": len(files), "rows_read": rows, "rows_accepted": accepted, "files_with_formal_trading_money": files_with_formal_money, "score_target_date": target.isoformat(), "knowledge_cutoff": knowledge_cutoff.isoformat(), "stocks_scored": scored, "idempotent_upsert": True, "source_revision_preserved": True, "secrets_included": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--score-date", type=date.fromisoformat)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    evidence = replay(args.raw_root, args.score_date)
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rows_read": evidence["rows_read"], "rows_accepted": evidence["rows_accepted"], "stocks_scored": evidence["stocks_scored"], "secrets_included": False}, ensure_ascii=False))
