"""Regressions for the NAS's repeated empty work and ingestion hot path."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

import app.ingestion as ingestion
from app.config import Settings
from app.finmind import FinMindClient, FinMindRequestBudget
from app.main import _score_evaluation_counts
from app.models import AccumulationScore, Base, BrokerDaily, HoldingDistribution, JobRun, PriceDaily, SourceRevision, Stock
from app.refresh_queue import AUTOMATIC_SELECTION_POLICY, _universe_budget_queue, queue_universe_budget_refresh
from app.scoring import BROKER_ROW_CONTRACT_VERSION, SCORE_VERSION
from test_per_stock_scoring_gate import END, FETCHED_AT, _seed_complete_sources


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def seed_stock(db, sid="9001"):
    db.add(Stock(stock_id=sid, stock_name=sid, market="上市", is_common_stock=True))
    db.commit()


def test_new_universe_stocks_are_flushed_before_revision_foreign_keys(db):
    rows = [{"stock_id": sid, "stock_name": sid, "type": "twse", "date": END.isoformat()} for sid in ("2330", "7812")]
    metrics = {}
    assert ingestion.ingest_records(db, "TaiwanStockInfo", rows, metrics=metrics) == 2
    assert metrics == {"accepted_count": 2, "versioned_count": 2}
    assert db.scalar(select(func.count()).select_from(Stock)) == 2
    assert db.scalar(select(func.count()).select_from(SourceRevision)) == 2
    assert ingestion.ingest_records(db, "TaiwanStockInfo", rows, metrics=metrics) == 2
    assert metrics["versioned_count"] == 0


def test_parent_flush_keeps_whole_universe_batch_rollback_atomic(db, monkeypatch):
    original = ingestion._record_revision
    def fail_second(db, dataset, row, *args):
        if row["stock_id"] == "7812":
            raise RuntimeError("simulated invalid second stock")
        return original(db, dataset, row, *args)
    monkeypatch.setattr(ingestion, "_record_revision", fail_second)
    with pytest.raises(RuntimeError):
        ingestion.ingest_records(db, "TaiwanStockInfo", [
            {"stock_id": sid, "type": "twse", "date": END.isoformat()} for sid in ("2330", "7812")
        ])
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Stock)) == 0
    assert db.scalar(select(func.count()).select_from(SourceRevision)) == 0


def test_batch_counts_revisions_exactly_without_scanning_history(db):
    seed_stock(db)
    statements = []
    def capture(_conn, _cursor, sql, *_):
        statements.append(sql.lower())
    event.listen(db.get_bind(), "before_cursor_execute", capture)
    row = {"stock_id": "9001", "date": END.isoformat(), "close": 100, "TradingVolume": 1000}
    metrics = {}
    assert ingestion.ingest_records(db, "TaiwanStockPrice", [row, row], metrics=metrics) == 2
    assert metrics["versioned_count"] == 1  # Duplicate provider rows are one revision.
    ingestion.ingest_records(db, "TaiwanStockPrice", [row], metrics=metrics)
    assert metrics["versioned_count"] == 0
    ingestion.ingest_records(db, "TaiwanStockPrice", [{**row, "close": 101}], metrics=metrics)
    assert metrics["versioned_count"] == 1
    assert not any("count(" in sql and "source_revisions" in sql for sql in statements)
    assert db.scalar(select(PriceDaily.close)) == 101
    assert db.scalar(select(func.count()).select_from(SourceRevision)) == 2


def test_empty_attempts_rotate_behind_untouched_stocks_across_hours(db):
    for sid in ("1230", "2330", "2408"):
        seed_stock(db, sid)
    db.add(PriceDaily(stock_id="2330", source_date=END, close=100, source_dataset="TaiwanStockPrice", fetched_at=FETCHED_AT))
    db.add(JobRun(dataset=ingestion.TARGETED_STOCK_SYNC_DATASET, status="DATA_INSUFFICIENT",
                  started_at=FETCHED_AT, finished_at=FETCHED_AT, requested_end_date=END,
                  checkpoint_state={"stock_id": "1230"}))
    db.commit()
    # Empty but untouched 2408 gets one chance, then data-bearing 2330,
    # and only then the already-attempted empty 1230.
    assert _universe_budget_queue(db, target=END)[0] == ["2408", "2330", "1230"]
    db.add(JobRun(dataset=ingestion.TARGETED_STOCK_SYNC_DATASET, status="DATA_INSUFFICIENT",
                  started_at=FETCHED_AT + timedelta(hours=1), requested_end_date=END,
                  checkpoint_state={"stock_id": "2408"}))
    db.commit()
    assert _universe_budget_queue(db, target=END)[0] == ["2330", "1230", "2408"]
    # A new source date and explicit manual refresh retain missing-data priority.
    assert _universe_budget_queue(db, target=END + timedelta(days=1))[0][0] == "1230"
    assert _universe_budget_queue(db)[0][0] == "1230"


def test_automatic_restart_reorders_tail_preserves_current_progress_and_budget(db, monkeypatch, tmp_path):
    for sid in ("1230", "2330", "2408"):
        seed_stock(db, sid)
    job = JobRun(dataset=ingestion.UNIVERSE_BUDGET_REFRESH_DATASET, status="QUEUED",
                 requested_end_date=END, started_at=FETCHED_AT,
                 checkpoint_state={"trigger": "closed_market_hourly", "stock_ids": ["1230", "2330", "2408"],
                                   "queue_index": 1, "current_stock_id": "2330", "stocks_completed": 1,
                                   "current_stock_progress": {"stock_id": "2330", "datasets": {"TaiwanStockPrice": {"refresh_complete": True}}},
                                   "next_retry_at": (FETCHED_AT + timedelta(hours=1)).isoformat()})
    db.add(job)
    db.commit()
    monkeypatch.setattr(ingestion, "_now", lambda: FETCHED_AT)
    monkeypatch.setattr(ingestion, "market_session_state", lambda: {"state": "CLOSED"})
    monkeypatch.setattr(ingestion, "closed_market_target_date", lambda *_: END)
    budget = FinMindRequestBudget(3500, tmp_path / "budget.json", used=3477)
    asyncio.run(ingestion.resume_universe_budget_refresh_job(db, SimpleNamespace(request_budget=budget), job))
    assert job.checkpoint_state["selection_policy"] == AUTOMATIC_SELECTION_POLICY
    assert job.checkpoint_state["queue_index"] == 1
    assert job.checkpoint_state["stock_ids"] == ["1230", "2330", "2408"]
    assert job.checkpoint_state["current_stock_progress"]["datasets"]["TaiwanStockPrice"]["refresh_complete"]
    assert job.checkpoint_state["budget"]["used"] == 3477


def test_incremental_refresh_only_fetches_missing_broker_day(db, monkeypatch, tmp_path):
    seed_stock(db)
    _seed_complete_sources(db, "9001")
    db.query(BrokerDaily).filter_by(stock_id="9001", source_date=END).delete()
    db.commit()
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0))
    calls = []
    def fetch(dataset, stock_id, source_date, end_date, **_):
        assert dataset == "TaiwanStockTradingDailyReport" and source_date == end_date
        calls.append((stock_id, source_date))
        return [{"stock_id": stock_id, "date": source_date, "securities_trader_id": "A", "buy_volume": 100,
                 "sell_volume": 10, "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION}], {"attempt": 1, "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION}
    monkeypatch.setattr(client, "fetch", fetch)
    result = asyncio.run(ingestion.fetch_and_score_stock(db, client, "9001", END,
                                                       reuse_broker_observations=True, allow_score_fallback=False))
    assert calls == [("9001", END.isoformat())]
    assert result["target_readiness"]["ready"]
    assert result["score"]["score"] is not None
    assert result["datasets"]["TaiwanStockTradingDailyReport"]["physical_requests"] == 1
    assert all(value["refresh_complete"] for value in result["datasets"].values())
    assert result["datasets"]["TaiwanStockPrice"]["physical_requests"] == 0


def test_complete_local_inputs_get_current_score_without_provider_work(db):
    seed_stock(db)
    _seed_complete_sources(db, "9001")
    class NoNetwork:
        settings = SimpleNamespace(finmind_api_token="configured")
        def provider_quota(self, **_):
            pytest.fail("local scoring must not probe quota")
    result = asyncio.run(ingestion.fetch_and_score_stock(db, NoNetwork(), "9001", END, allow_score_fallback=False))
    assert result["score"]["score"] is not None
    assert result["score"]["score_version"] == SCORE_VERSION
    assert all(value["physical_requests"] == 0 and value["refresh_complete"] for value in result["datasets"].values())


@pytest.mark.parametrize("invalid_kind", ["legacy_contract", "null_net"])
def test_mixed_valid_invalid_broker_day_is_refetched(db, monkeypatch, tmp_path, invalid_kind):
    seed_stock(db)
    _seed_complete_sources(db, "9001")
    db.add(BrokerDaily(stock_id="9001", source_date=END, securities_trader_id="OLD",
                       net_volume=10 if invalid_kind == "legacy_contract" else None,
                       provider_row_validated=invalid_kind != "legacy_contract",
                       provider_row_contract_version=BROKER_ROW_CONTRACT_VERSION,
                       source_dataset="TaiwanStockTradingDailyReport", fetched_at=FETCHED_AT))
    db.commit()
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0))
    calls = []
    def fetch(dataset, sid, start, end, **_):
        calls.append((start, end))
        return [{"stock_id": sid, "date": start, "securities_trader_id": "OLD", "buy_volume": 11,
                 "sell_volume": 1, "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION}], {
                     "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION}
    monkeypatch.setattr(client, "fetch", fetch)
    result = asyncio.run(ingestion.fetch_and_score_stock(db, client, "9001", END,
                                                       reuse_broker_observations=True, allow_score_fallback=False))
    assert calls == [(END.isoformat(), END.isoformat())]
    assert result["target_readiness"]["ready"]


def test_stale_week_is_fetched_once_without_zero_request_scoring_loop(db, monkeypatch, tmp_path):
    seed_stock(db)
    _seed_complete_sources(db, "9001")
    for row in db.scalars(select(HoldingDistribution).order_by(HoldingDistribution.source_date)).all():
        row.source_date -= timedelta(days=7)
        db.flush([row])
    db.commit()
    monkeypatch.setattr(ingestion, "closed_market_target_date", lambda *_: END)
    monkeypatch.setattr(ingestion, "market_session_state", lambda: {"state": "CLOSED"})
    job, _ = queue_universe_budget_refresh(db, END, automatic=True)
    client = SimpleNamespace(settings=SimpleNamespace(broker_quota_reserve=0),
                             request_budget=FinMindRequestBudget(3500, tmp_path / "stale-budget.json"),
                             provider_quota=lambda **_: {"provider_reported_remaining": 6000})
    calls = []
    async def fetch(ids, dataset, start, end, **kwargs):
        assert dataset == "TaiwanStockHoldingSharesPer"
        calls.append((ids, start, end))
        assert len(calls) == 1, "stale weekly source must not loop in the same job"
        client.request_budget.reserve()
        return {"success": 1, "physical_requests": 1, "rows_received": 0}
    client.fetch_stocks_dataset = fetch
    result = asyncio.run(ingestion.resume_universe_budget_refresh_job(db, client, job))
    assert len(calls) == 1
    assert result["phase"] == "waiting_for_source_data"
    assert result["budget"]["used"] == 1
    assert result["daily_completion"]["pending_count"] == 1


def test_summary_distinguishes_unscored_from_evaluated_insufficient(db):
    for sid in ("1230", "2330", "2408"):
        seed_stock(db, sid)
    for sid, version in (("1230", SCORE_VERSION), ("2330", "old-version")):
        db.add(AccumulationScore(stock_id=sid, source_date=END, score=None, status="DATA_INSUFFICIENT",
                                 score_version=version, calculated_at=FETCHED_AT, knowledge_cutoff=FETCHED_AT))
    db.commit()
    assert _score_evaluation_counts(db, 3, 3) == {
        "evaluated_stock_count": 1, "pending_evaluation_count": 2, "evaluated_insufficient_stock_count": 1,
    }
