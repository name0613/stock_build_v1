from __future__ import annotations

from collections.abc import Generator

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
    timestamp_type = "TIMESTAMPTZ" if engine.dialect.name == "postgresql" else "DATETIME"
    json_type = "JSONB" if engine.dialect.name == "postgresql" else "JSON"
    additive_columns = {
        "accumulation_features": {"knowledge_cutoff": timestamp_type, "input_snapshot_hash": "VARCHAR(64)"},
        "accumulation_scores": {"knowledge_cutoff": timestamp_type, "input_snapshot_hash": "VARCHAR(64)", "input_source_hashes": json_type, "formula_hash": "VARCHAR(64)"},
        "data_sync_status": {"last_attempt_at": timestamp_type, "last_fetch_at": timestamp_type, "usable_records": "INTEGER DEFAULT 0", "stored_records": "INTEGER DEFAULT 0", "staleness_state": "VARCHAR(32)"},
        "job_runs": {"requested_start_date": "DATE", "requested_end_date": "DATE", "error_code": "VARCHAR(64)", "stocks_attempted": "INTEGER DEFAULT 0", "stocks_completed": "INTEGER DEFAULT 0", "stocks_failed": "INTEGER DEFAULT 0", "checkpoint_state": json_type},
        "score_versions": {"manifest_hash": "VARCHAR(64)"},
    }
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in additive_columns.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
    if engine.dialect.name == "postgresql":
        # Additive migration for deployments created by the first release.  The
        # old uniqueness constraints are replaced so a later knowledge cutoff
        # is an explicit new calculation rather than an overwrite.
        statements = [
            "ALTER TABLE accumulation_features ADD COLUMN IF NOT EXISTS knowledge_cutoff TIMESTAMPTZ",
            "ALTER TABLE accumulation_features ADD COLUMN IF NOT EXISTS input_snapshot_hash VARCHAR(64)",
            "ALTER TABLE accumulation_scores ADD COLUMN IF NOT EXISTS knowledge_cutoff TIMESTAMPTZ",
            "ALTER TABLE accumulation_scores ADD COLUMN IF NOT EXISTS input_snapshot_hash VARCHAR(64)",
            "ALTER TABLE accumulation_scores ADD COLUMN IF NOT EXISTS input_source_hashes JSONB DEFAULT '[]'::jsonb",
            "ALTER TABLE accumulation_scores ADD COLUMN IF NOT EXISTS formula_hash VARCHAR(64)",
            "ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ",
            "ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS last_fetch_at TIMESTAMPTZ",
            "ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS usable_records INTEGER DEFAULT 0",
            "ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS stored_records INTEGER DEFAULT 0",
            "ALTER TABLE data_sync_status ADD COLUMN IF NOT EXISTS staleness_state VARCHAR(32)",
            "ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS requested_start_date DATE",
            "ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS requested_end_date DATE",
            "ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS error_code VARCHAR(64)",
            "ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS stocks_attempted INTEGER DEFAULT 0",
            "ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS stocks_completed INTEGER DEFAULT 0",
            "ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS stocks_failed INTEGER DEFAULT 0",
            "ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS checkpoint_state JSONB DEFAULT '{}'::jsonb",
            "ALTER TABLE score_versions ADD COLUMN IF NOT EXISTS manifest_hash VARCHAR(64)",
            "ALTER TABLE accumulation_features DROP CONSTRAINT IF EXISTS uq_features_stock_date",
            "ALTER TABLE accumulation_scores DROP CONSTRAINT IF EXISTS uq_score_stock_date_version",
            "ALTER TABLE accumulation_features ADD CONSTRAINT uq_features_stock_date_cutoff UNIQUE (stock_id, source_date, knowledge_cutoff)",
            "ALTER TABLE accumulation_scores ADD CONSTRAINT uq_score_stock_date_version_cutoff UNIQUE (stock_id, source_date, score_version, knowledge_cutoff)",
        ]
        with engine.begin() as connection:
            for statement in statements:
                try:
                    connection.execute(text(statement))
                except Exception:
                    # Existing constraint names can already be migrated; the
                    # next statement remains safe and startup must continue.
                    pass
    if engine.dialect.name == "sqlite":
        columns = {column["name"] for column in inspect(engine).get_columns("holding_distribution")}
        if "shares" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE holding_distribution ADD COLUMN shares FLOAT"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
