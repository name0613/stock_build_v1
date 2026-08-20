from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, text
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


def _apply_versioned_migrations() -> None:
    """Apply checked-in PostgreSQL migrations; any failure aborts startup."""
    migration_dir = Path(__file__).resolve().parents[2] / "migrations"
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


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
