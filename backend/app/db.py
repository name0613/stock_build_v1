from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

settings = get_settings()
engine = create_engine(
    settings.resolved_database_url(),
    connect_args={"check_same_thread": False} if settings.resolved_database_url().startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "postgresql":
        _apply_versioned_migrations()
    elif engine.dialect.name == "sqlite":
        _apply_sqlite_compatibility_migrations()


def _apply_versioned_migrations() -> None:
    """Apply checked-in PostgreSQL migrations; any failure aborts startup."""
    source_root = Path(__file__).resolve()
    candidates = (source_root.parents[2] / "migrations", source_root.parents[1] / "migrations")
    migration_dir = next((path for path in candidates if path.exists()), None)
    if migration_dir is None:
        raise RuntimeError("checked-in migrations directory is missing")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"))
        applied = {row[0] for row in connection.execute(text("SELECT version FROM schema_migrations"))}
        for path in sorted(migration_dir.glob("*.sql")):
            version = path.stem
            if version == "001_init" and version not in applied:
                connection.execute(text("INSERT INTO schema_migrations(version) VALUES (:version)"), {"version": version})
                applied.add(version)
                continue
            if version in applied:
                continue
            connection.execute(text(path.read_text(encoding="utf-8")))
            connection.execute(text("INSERT INTO schema_migrations(version) VALUES (:version)"), {"version": version})


def _apply_sqlite_compatibility_migrations() -> None:
    """Keep the local test/development SQLite schema aligned with PostgreSQL."""
    additions = {
        "institutional_daily": {"dealer_aggregate_net": "FLOAT"},
        "accumulation_features": {"knowledge_cutoff": "DATETIME", "input_snapshot_hash": "VARCHAR(64)"},
        "accumulation_scores": {"knowledge_cutoff": "DATETIME", "input_snapshot_hash": "VARCHAR(64)", "input_source_hashes": "JSON", "formula_hash": "VARCHAR(64)"},
        "data_sync_status": {"last_attempt_at": "DATETIME", "last_fetch_at": "DATETIME", "last_http_success_at": "DATETIME", "last_fully_successful_sync": "DATETIME", "last_usable_data_at": "DATETIME", "usable_records": "INTEGER DEFAULT 0", "stored_records": "INTEGER DEFAULT 0", "staleness_state": "VARCHAR(32)", "attempt_latest_source_date": "DATE", "expected_latest_source_date": "DATE", "source_age_days": "INTEGER", "rows_received_this_attempt": "INTEGER DEFAULT 0", "rows_accepted_this_attempt": "INTEGER DEFAULT 0", "rows_rejected_this_attempt": "INTEGER DEFAULT 0", "rows_versioned_this_attempt": "INTEGER DEFAULT 0", "observations_reused_this_attempt": "INTEGER DEFAULT 0", "stored_rows_total": "INTEGER DEFAULT 0"},
        "job_runs": {"requested_start_date": "DATE", "requested_end_date": "DATE", "error_code": "VARCHAR(64)", "stocks_attempted": "INTEGER DEFAULT 0", "stocks_completed": "INTEGER DEFAULT 0", "stocks_failed": "INTEGER DEFAULT 0", "checkpoint_state": "JSON"},
        "score_versions": {"manifest_hash": "VARCHAR(64)"},
        "broker_daily": {"provider_report_complete": "BOOLEAN DEFAULT FALSE", "provider_contract_version": "VARCHAR(100)"},
    }
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
