from __future__ import annotations

import asyncio

from app import ingestion


class _Dialect:
    name = "postgresql"


class _Connection:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def scalar(self, statement, parameters):
        self.calls.append((str(statement), parameters))
        return self.acquired if "pg_try_advisory_lock" in str(statement) else True


class _Bind:
    dialect = _Dialect()

    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self) -> _Connection:
        return self.connection


class _Database:
    def __init__(self, acquired: bool) -> None:
        self.connection = _Connection(acquired)
        self.bind = _Bind(self.connection)

    def get_bind(self) -> _Bind:
        return self.bind


def test_pipeline_advisory_lock_skips_second_process_before_work(monkeypatch) -> None:
    database = _Database(acquired=False)
    called = False

    async def inner(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "SUCCESS"}

    monkeypatch.setattr(ingestion, "_catch_up_locked", inner)
    result = asyncio.run(ingestion.catch_up(database, object()))
    assert result["status"] == "SKIPPED_CONCURRENT_RUN"
    assert result["reason_code"] == "PIPELINE_ADVISORY_LOCK_HELD"
    assert called is False
    assert len(database.connection.calls) == 1


def test_pipeline_advisory_lock_is_released_after_work(monkeypatch) -> None:
    database = _Database(acquired=True)

    async def inner(*_args, **_kwargs):
        return {"status": "SUCCESS"}

    monkeypatch.setattr(ingestion, "_catch_up_locked", inner)
    result = asyncio.run(ingestion.catch_up(database, object()))
    assert result["status"] == "SUCCESS"
    assert ["pg_try_advisory_lock" in call[0] for call in database.connection.calls] == [True, False]
    assert database.connection.calls[1][1] == {"lock_key": ingestion.PIPELINE_ADVISORY_LOCK_KEY}
