from __future__ import annotations

import asyncio
import json
from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.finmind import BROKER_ROW_CONTRACT_VERSION, FinMindClient, FinMindError
from app.ingestion import holding_coverage_state, record_score_blocked, score_source_coverage_gate
from app.models import AccumulationScore, Base, DataSyncStatus, JobRun, Stock


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _ready_sync(db: Session, *, broker_pending: int = 0, holding_complete: bool = True) -> None:
    target = date(2026, 8, 24)
    for dataset in (
        "TaiwanStockInfo",
        "TaiwanStockInstitutionalInvestorsBuySellWide",
        "TaiwanStockShareholding",
        "TaiwanStockHoldingSharesPer",
        "TaiwanStockTradingDailyReport",
        "TaiwanStockPrice",
    ):
        metadata = {}
        status = "SUCCESS"
        if dataset == "TaiwanStockHoldingSharesPer":
            metadata = {"coverage": {"holding_schema": {"complete": holding_complete, "required_bucket_count": 15}}}
        if dataset == "TaiwanStockTradingDailyReport" and broker_pending:
            status = "PARTIAL"
            metadata = {"coverage": {"retryable_pending": broker_pending}}
        db.add(DataSyncStatus(dataset=dataset, status=status, records=1, latest_source_date=target, expected_latest_source_date=target, staleness_state="FRESH", metadata_json=metadata))
    db.commit()


def test_score_block_is_persisted_without_iterating_stock_universe() -> None:
    db = _db()
    db.add(Stock(stock_id="2330", stock_name="Test", market="上市", security_type="股票", is_common_stock=True))
    db.commit()
    _ready_sync(db, broker_pending=7)

    gate = score_source_coverage_gate(db, date(2026, 8, 24))
    assert gate["status"] == "SCORE_BLOCKED_BY_SOURCE_COVERAGE"
    assert gate["reason_code"] == "BROKER_RETRY_PENDING"
    assert gate["score_rows_processed"] == 0
    assert record_score_blocked(db, date(2026, 8, 24), gate) == {"SCORE_BLOCKED_BY_SOURCE_COVERAGE": 0}

    job = db.scalar(select(JobRun).where(JobRun.dataset == "score"))
    assert job is not None
    assert job.status == "SCORE_BLOCKED_BY_SOURCE_COVERAGE"
    assert job.stocks_attempted == 0
    assert db.scalars(select(AccumulationScore)).all() == []


def test_broker_quota_reserve_bounds_pending_work(monkeypatch, tmp_path) -> None:
    settings = Settings(raw_root=tmp_path, finmind_api_token="configured", broker_max_retries=0, broker_concurrency=1, broker_quota_reserve=2)
    client = FinMindClient(settings)
    calls: list[str] = []
    monkeypatch.setattr(client, "provider_quota", lambda **_: {"provider_reported_limit_per_hour": 6_000, "provider_reported_remaining": 3})

    def fetch(stock_id: str, *_args, **_kwargs):
        calls.append(stock_id)
        return ([{"stock_id": stock_id, "date": "2026-08-24", "securities_trader_id": "A", "buy": 10, "sell": 1, "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION}], {"attempt": 1, "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION})

    monkeypatch.setattr(client, "fetch", fetch)
    result = asyncio.run(client.fetch_broker_stocks(["2330", "2317", "1101"], "2026-08-24", "2026-08-24"))
    assert result["quota_probe_status"] == "PASS"
    assert result["quota_reserve"] == 2
    assert result["usable_quota"] == 1
    assert result["selected_pending_count"] == 1
    assert len(calls) == 1
    assert result["retryable_pending"] == 2


def test_quota_equal_to_reserve_stops_without_provider_work(monkeypatch, tmp_path) -> None:
    settings = Settings(raw_root=tmp_path, finmind_api_token="configured", broker_max_retries=0, broker_quota_reserve=2)
    client = FinMindClient(settings)
    calls: list[str] = []
    monkeypatch.setattr(client, "provider_quota", lambda **_: {"provider_reported_limit_per_hour": 6_000, "provider_reported_remaining": 2})
    monkeypatch.setattr(client, "fetch", lambda stock_id, *_args, **_kwargs: calls.append(stock_id))

    result = asyncio.run(client.fetch_broker_stocks(["2330"], "2026-08-24", "2026-08-24"))
    assert result["fatal_code"] == "QUOTA_EXHAUSTED"
    assert result["quota_blocked"] is True
    assert calls == []


def test_retryable_broker_failure_is_deferred_and_checkpointed(monkeypatch, tmp_path) -> None:
    settings = Settings(raw_root=tmp_path, broker_max_retries=0, broker_concurrency=1, broker_retry_base_seconds=60)
    client = FinMindClient(settings)
    monkeypatch.setattr(client, "fetch", lambda *_args, **_kwargs: (_ for _ in ()).throw(FinMindError("TIMEOUT", "temporary")))
    first = asyncio.run(client.fetch_broker_stocks(["2330"], "2026-08-24", "2026-08-24"))
    assert first["retryable_failed"] == 1
    checkpoint_file = next((tmp_path / "checkpoints").glob("TaiwanStockTradingDailyReport-*.json"))
    failure = json.loads(checkpoint_file.read_text(encoding="utf-8"))["failed"][0]
    assert failure["retry_class"] == "transient_provider_or_network"
    assert failure["next_eligible_retry_at"] is not None

    calls: list[str] = []
    monkeypatch.setattr(client, "fetch", lambda stock_id, *_args, **_kwargs: calls.append(stock_id))
    second = asyncio.run(client.fetch_broker_stocks(["2330"], "2026-08-24", "2026-08-24"))
    assert second["retry_deferred"] == 1
    assert calls == []


def test_holding_publication_wait_is_throttled_for_the_same_target(monkeypatch, tmp_path) -> None:
    client = FinMindClient(Settings(raw_root=tmp_path, holding_publication_check_interval_hours=24))

    def unpublished(*_args, **_kwargs):
        return [], {"empty_is_valid": True, "empty_reason": "no_provider_observation", "empty_observation_dates": ["2026-08-21"]}

    monkeypatch.setattr(client, "fetch", unpublished)
    first = asyncio.run(client.fetch_stocks_dataset(["2330"], "TaiwanStockHoldingSharesPer", "2026-08-21", "2026-08-21"))
    assert first["publication_state"] == "WAITING_FOR_PROVIDER_PUBLICATION"
    assert first["physical_requests"] == 1

    def must_not_retry(*_args, **_kwargs):
        raise AssertionError("publication check was not throttled")

    monkeypatch.setattr(client, "fetch", must_not_retry)
    second = asyncio.run(client.fetch_stocks_dataset(["2330"], "TaiwanStockHoldingSharesPer", "2026-08-21", "2026-08-21"))
    assert second["publication_check_deferred"] is True
    assert second["physical_requests"] == 0


def test_holding_coverage_requires_all_fifteen_standard_buckets() -> None:
    db = _db()
    db.add(Stock(stock_id="2330", stock_name="Test", market="上市", security_type="股票", is_common_stock=True))
    db.commit()
    state = holding_coverage_state(db, ["2330"], date(2026, 8, 21))
    assert state["required_bucket_count"] == 15
    assert state["complete"] is False
    assert state["incomplete_stocks"] == 1
