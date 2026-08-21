from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.ingestion import SYNC_COUNTER_SEMANTICS_VERSION, _mark_sync
from app.models import Base, DataSyncStatus
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_current_attempt_counters_are_versioned_and_exactly_reconciled() -> None:
    db = _session()
    _mark_sync(
        db,
        "TaiwanStockPrice",
        "PARTIAL",
        7,
        date(2026, 8, 20),
        rows_received=10,
        rows_accepted=7,
        rows_rejected=3,
        rows_versioned=5,
        observations_reused=20,
        physical_requests=2,
    )
    row = db.get(DataSyncStatus, "TaiwanStockPrice")
    assert row is not None
    assert row.counter_semantics_version == SYNC_COUNTER_SEMANTICS_VERSION
    assert row.counters_are_current_attempt is True
    assert len(row.counter_attempt_id or "") == 32
    assert row.physical_requests_this_attempt == 2
    assert row.rows_received_this_attempt == row.rows_accepted_this_attempt + row.rows_rejected_this_attempt
    assert row.rows_versioned_this_attempt <= row.rows_accepted_this_attempt


def test_current_attempt_rejects_unreconciled_rows() -> None:
    db = _session()
    with pytest.raises(ValueError, match="must equal received"):
        _mark_sync(db, "TaiwanStockPrice", "PARTIAL", 7, None, rows_received=10, rows_accepted=7, rows_rejected=0)


def test_postgres_migration_preserves_then_resets_unversioned_counters() -> None:
    sql = (Path(__file__).resolve().parents[2] / "migrations/009_version_sync_attempt_counters.sql").read_text(encoding="utf-8")
    assert "legacy_pre_v5_counter_snapshot" in sql
    assert "ALTER COLUMN metadata_json TYPE JSONB" in sql
    assert "USING COALESCE(metadata_json::jsonb, '{}'::jsonb)" in sql
    assert "rows_received_this_attempt = 0" in sql
    assert "rows_accepted_this_attempt = 0" in sql
    assert "counter_semantics_version = 'legacy-pre-v5-reset-v1'" in sql
    assert "counters_are_current_attempt = FALSE" in sql
