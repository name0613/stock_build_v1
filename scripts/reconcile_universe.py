from __future__ import annotations

import glob
from pathlib import Path

import pyarrow.parquet as parquet
from sqlalchemy import select

from backend.app.db import SessionLocal, init_db
from backend.app.ingestion import ingest_records, normalize_stock
from backend.app.models import Stock


if __name__ == "__main__":
    init_db()
    files = sorted(glob.glob("data/raw/TaiwanStockInfo/date=*/part-*.parquet"))
    if not files:
        raise SystemExit("No raw TaiwanStockInfo parquet evidence found")
    source_file = max(files, key=lambda path: parquet.read_metadata(path).num_rows)
    rows = parquet.read_table(source_file).to_pylist()
    db = SessionLocal()
    count = ingest_records(db, "TaiwanStockInfo", rows)
    active_ids = {normalized["stock_id"] for row in rows if (normalized := normalize_stock(row)) is not None}
    for stock in db.scalars(select(Stock).where(Stock.is_common_stock.is_(True))).all():
        if stock.stock_id not in active_ids:
            stock.is_common_stock = False
    db.commit()
    print({"active_common_stocks": len(active_ids), "rows_upserted": count, "raw_file": str(Path(source_file))})
    db.close()
