from __future__ import annotations

from backend.app.db import SessionLocal, init_db
from backend.app.models import DataSyncStatus
from sqlalchemy import select


if __name__ == "__main__":
    init_db()
    db = SessionLocal()
    rows = db.scalars(select(DataSyncStatus).order_by(DataSyncStatus.dataset)).all()
    db.close()
    print({"status": "READ_ONLY", "datasets": [{"dataset": row.dataset, "status": row.status, "last_attempt_at": row.last_attempt_at, "last_fetch_at": row.last_fetch_at, "usable_records": row.usable_records, "staleness": row.staleness_state} for row in rows], "note": "No sync success is inferred from table contents"})
