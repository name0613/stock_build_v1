from __future__ import annotations

from app import db as db_module


class _Connection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, dict | None]] = []

    def execute(self, statement, parameters=None):
        self.statements.append((str(statement), parameters))
        return []


class _Begin:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(self, *_args) -> bool:
        return False


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def begin(self) -> _Begin:
        return _Begin(self.connection)


def test_postgres_migration_lock_precedes_schema_state_read(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(db_module, "engine", _Engine(connection))
    db_module._apply_versioned_migrations()
    statements = [sql for sql, _ in connection.statements]
    lock_index = next(index for index, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql)
    state_read_index = next(index for index, sql in enumerate(statements) if "SELECT version FROM schema_migrations" in sql)
    assert lock_index < state_read_index
    assert connection.statements[lock_index][1] == {"lock_key": db_module.MIGRATION_ADVISORY_LOCK_KEY}
