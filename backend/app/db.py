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
MIGRATION_ADVISORY_LOCK_KEY = 8_202_608_210_008


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
        # API and worker start together. Serialize their migration transaction
        # before either process reads schema_migrations, then release on commit.
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": MIGRATION_ADVISORY_LOCK_KEY})
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
        "stocks": {"is_favorite": "BOOLEAN DEFAULT FALSE"},
        "institutional_daily": {"dealer_aggregate_net": "FLOAT"},
        "accumulation_features": {"knowledge_cutoff": "DATETIME", "input_snapshot_hash": "VARCHAR(64)"},
        "accumulation_scores": {"knowledge_cutoff": "DATETIME", "input_snapshot_hash": "VARCHAR(64)", "input_source_hashes": "JSON", "formula_hash": "VARCHAR(64)"},
        "data_sync_status": {"last_attempt_at": "DATETIME", "last_fetch_at": "DATETIME", "last_http_success_at": "DATETIME", "last_fully_successful_sync": "DATETIME", "last_usable_data_at": "DATETIME", "usable_records": "INTEGER DEFAULT 0", "stored_records": "INTEGER DEFAULT 0", "staleness_state": "VARCHAR(32)", "attempt_latest_source_date": "DATE", "expected_latest_source_date": "DATE", "source_age_days": "INTEGER", "rows_received_this_attempt": "INTEGER DEFAULT 0", "rows_accepted_this_attempt": "INTEGER DEFAULT 0", "rows_rejected_this_attempt": "INTEGER DEFAULT 0", "rows_versioned_this_attempt": "INTEGER DEFAULT 0", "observations_reused_this_attempt": "INTEGER DEFAULT 0", "physical_requests_this_attempt": "INTEGER DEFAULT 0", "stored_rows_total": "INTEGER DEFAULT 0", "counter_attempt_id": "VARCHAR(64)", "counter_semantics_version": "VARCHAR(64) DEFAULT 'attempt-v5-reconciled-v1'", "counters_are_current_attempt": "BOOLEAN DEFAULT FALSE"},
        "job_runs": {"requested_start_date": "DATE", "requested_end_date": "DATE", "error_code": "VARCHAR(64)", "stocks_attempted": "INTEGER DEFAULT 0", "stocks_completed": "INTEGER DEFAULT 0", "stocks_failed": "INTEGER DEFAULT 0", "checkpoint_state": "JSON"},
        "score_versions": {"manifest_hash": "VARCHAR(64)"},
        "broker_daily": {"provider_report_complete": "BOOLEAN DEFAULT FALSE", "provider_contract_version": "VARCHAR(100)", "provider_row_validated": "BOOLEAN DEFAULT FALSE", "provider_row_contract_version": "VARCHAR(100)"},
        "price_daily": {"trading_money": "FLOAT", "trading_turnover": "FLOAT"},
    }
    with engine.begin() as connection:
        inspector = inspect(connection)
        # Recover an interrupted compatibility rebuild from an older local
        # database.  The legacy table is created only by this function, so a
        # zero-row destination can be safely completed before normal checks.
        tables = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        if "accumulation_features_legacy_v1" in tables and "accumulation_features" in tables:
            destination_count = int(connection.execute(text("SELECT count(*) FROM accumulation_features")).scalar_one())
            if destination_count == 0:
                for index in connection.execute(text("PRAGMA index_list('accumulation_features_legacy_v1')")).all():
                    index_name = str(index[1])
                    if not index_name.startswith("sqlite_autoindex_"):
                        connection.execute(text(f"DROP INDEX IF EXISTS '{index_name}'"))
                legacy_columns = {column[1] for column in connection.execute(text("PRAGMA table_info('accumulation_features_legacy_v1')")).all()}
                knowledge_cutoff = "knowledge_cutoff" if "knowledge_cutoff" in legacy_columns else "NULL"
                input_snapshot_hash = "input_snapshot_hash" if "input_snapshot_hash" in legacy_columns else "NULL"
                connection.execute(text(f"INSERT INTO accumulation_features (stock_id, source_date, \"values\", coverage, latest_source_date, calculated_at, knowledge_cutoff, input_snapshot_hash) SELECT stock_id, source_date, \"values\", coverage, latest_source_date, calculated_at, {knowledge_cutoff}, {input_snapshot_hash} FROM accumulation_features_legacy_v1"))
                connection.execute(text("DROP TABLE accumulation_features_legacy_v1"))
        for table, columns in additions.items():
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, sql_type in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
        # SQLite cannot alter a table-level UNIQUE constraint in place.  Some
        # early local databases used (stock_id, source_date) for features,
        # which would reject a second point-in-time snapshot.  Rebuild only
        # this known legacy table and copy every row before dropping the old
        # schema; fresh databases take the normal create_all path.
        indexes = connection.execute(text("PRAGMA index_list('accumulation_features')")).all()
        legacy_unique = False
        for index in indexes:
            if not index[2]:
                continue
            columns_for_index = [row[2] for row in connection.execute(text(f"PRAGMA index_info('{index[1]}')")).all()]
            if columns_for_index == ["stock_id", "source_date"]:
                legacy_unique = True
                break
        if legacy_unique:
            connection.execute(text("ALTER TABLE accumulation_features RENAME TO accumulation_features_legacy_v1"))
            for index in connection.execute(text("PRAGMA index_list('accumulation_features_legacy_v1')")).all():
                index_name = str(index[1])
                if not index_name.startswith("sqlite_autoindex_"):
                    connection.execute(text(f"DROP INDEX IF EXISTS '{index_name}'"))
            Base.metadata.create_all(bind=connection)
            legacy_columns = {column[1] for column in connection.execute(text("PRAGMA table_info('accumulation_features_legacy_v1')")).all()}
            knowledge_cutoff = "knowledge_cutoff" if "knowledge_cutoff" in legacy_columns else "NULL"
            input_snapshot_hash = "input_snapshot_hash" if "input_snapshot_hash" in legacy_columns else "NULL"
            connection.execute(text(f"INSERT INTO accumulation_features (stock_id, source_date, \"values\", coverage, latest_source_date, calculated_at, knowledge_cutoff, input_snapshot_hash) SELECT stock_id, source_date, \"values\", coverage, latest_source_date, calculated_at, {knowledge_cutoff}, {input_snapshot_hash} FROM accumulation_features_legacy_v1"))
            connection.execute(text("DROP TABLE accumulation_features_legacy_v1"))

        # Apply the same compatibility repair to scores.  Older SQLite
        # databases used (stock_id, source_date, score_version), which makes
        # a second knowledge-cutoff snapshot fail even though PostgreSQL has
        # already moved to the versioned constraint.
        score_tables = {row[0] for row in connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        if "accumulation_scores_legacy_v1" in score_tables and "accumulation_scores" in score_tables:
            destination_count = int(connection.execute(text("SELECT count(*) FROM accumulation_scores")).scalar_one())
            if destination_count == 0:
                legacy_columns = {column[1] for column in connection.execute(text("PRAGMA table_info('accumulation_scores_legacy_v1')")).all()}
                score_columns = ("stock_id", "source_date", "score", "status", "score_version", "components", "explanation", "coverage", "calculated_at", "knowledge_cutoff", "input_snapshot_hash", "input_source_hashes", "formula_hash")
                defaults = {"components": "'{}'", "explanation": "'[]'", "coverage": "'{}'", "input_source_hashes": "'[]'"}
                select_columns = ", ".join(f"COALESCE({column}, {defaults[column]})" if column in defaults and column in legacy_columns else column if column in legacy_columns else "NULL" for column in score_columns)
                connection.execute(text(f"INSERT INTO accumulation_scores ({', '.join(score_columns)}) SELECT {select_columns} FROM accumulation_scores_legacy_v1"))
                connection.execute(text("DROP TABLE accumulation_scores_legacy_v1"))
        score_indexes = connection.execute(text("PRAGMA index_list('accumulation_scores')")).all()
        legacy_score_unique = False
        for index in score_indexes:
            if not index[2]:
                continue
            columns_for_index = [row[2] for row in connection.execute(text(f"PRAGMA index_info('{index[1]}')")).all()]
            if columns_for_index == ["stock_id", "source_date", "score_version"]:
                legacy_score_unique = True
                break
        if legacy_score_unique:
            connection.execute(text("ALTER TABLE accumulation_scores RENAME TO accumulation_scores_legacy_v1"))
            for index in connection.execute(text("PRAGMA index_list('accumulation_scores_legacy_v1')")).all():
                index_name = str(index[1])
                if not index_name.startswith("sqlite_autoindex_"):
                    connection.execute(text(f"DROP INDEX IF EXISTS '{index_name}'"))
            Base.metadata.create_all(bind=connection)
            legacy_columns = {column[1] for column in connection.execute(text("PRAGMA table_info('accumulation_scores_legacy_v1')")).all()}
            score_columns = ("stock_id", "source_date", "score", "status", "score_version", "components", "explanation", "coverage", "calculated_at", "knowledge_cutoff", "input_snapshot_hash", "input_source_hashes", "formula_hash")
            defaults = {"components": "'{}'", "explanation": "'[]'", "coverage": "'{}'", "input_source_hashes": "'[]'"}
            select_columns = ", ".join(f"COALESCE({column}, {defaults[column]})" if column in defaults and column in legacy_columns else column if column in legacy_columns else "NULL" for column in score_columns)
            connection.execute(text(f"INSERT INTO accumulation_scores ({', '.join(score_columns)}) SELECT {select_columns} FROM accumulation_scores_legacy_v1"))
            connection.execute(text("DROP TABLE accumulation_scores_legacy_v1"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
