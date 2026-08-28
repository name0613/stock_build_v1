from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.calendar import CALENDAR_HASH
from app.ingestion import authoritative_source_state_hash, score_snapshot_state
from app.main import CURRENT_SCORE_DATASETS, app
from app.models import AccumulationScore, DataSyncStatus, JobRun, Stock
from app.scoring import FORMULA_HASH, SCORE_VERSION
from sqlalchemy import func, select


CURRENT_DATE = date(2026, 8, 20)
CURRENT_TIME = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)


def _clear_sync_status() -> None:
    with SessionLocal() as db:
        db.query(DataSyncStatus).delete()
        db.query(JobRun).filter(JobRun.dataset == "score").delete()
        db.commit()


@pytest.fixture(autouse=True)
def clean_sync_status(monkeypatch) -> None:
    # These API fixtures model a frozen snapshot.  Production evaluates the
    # live calendar policy, so keep the fixture policy clock deterministic.
    monkeypatch.setattr("app.main.authoritative_expected_latest_source_date", lambda _dataset: CURRENT_DATE)
    _clear_sync_status()
    yield
    _clear_sync_status()


def _seed_authoritative_sync(*, failing_dataset: str | None = None, status: str = "SUCCESS", error_code: str | None = None, current_date: date = CURRENT_DATE) -> None:
    with SessionLocal() as db:
        for dataset in CURRENT_SCORE_DATASETS:
            row = DataSyncStatus(
                dataset=dataset,
                status=status if dataset == failing_dataset else "SUCCESS",
                latest_source_date=current_date,
                last_successful_sync=CURRENT_TIME,
                last_http_success_at=CURRENT_TIME,
                last_fully_successful_sync=CURRENT_TIME,
                last_usable_data_at=CURRENT_TIME,
                last_attempt_at=CURRENT_TIME,
                last_fetch_at=CURRENT_TIME,
                usable_records=1,
                stored_records=1,
                staleness_state="ERROR" if dataset == failing_dataset and status == "FAILED" else ("PARTIAL" if dataset == failing_dataset else "FRESH"),
                attempt_latest_source_date=current_date,
                expected_latest_source_date=current_date,
                last_error_code=error_code if dataset == failing_dataset else None,
                metadata_json={"coverage": {"holding_schema": {"complete": True, "required_bucket_count": 15}}} if dataset == "TaiwanStockHoldingSharesPer" else {},
            )
            db.add(row)
        db.commit()


def _seed_score_job(*, status: str = "SUCCESS", checkpoint_overrides: dict | None = None) -> None:
    with SessionLocal() as db:
        stock_count = db.scalar(select(func.count()).select_from(Stock).where(Stock.is_common_stock.is_(True))) or 0
        checkpoint = {
            "target_date": CURRENT_DATE.isoformat(),
            "score_version": SCORE_VERSION,
            "formula_hash": FORMULA_HASH,
            "calendar_hash": CALENDAR_HASH,
            "source_state_hash": authoritative_source_state_hash(db),
            "stock_count": stock_count,
            **score_snapshot_state(db, CURRENT_DATE),
            **(checkpoint_overrides or {}),
        }
        db.add(JobRun(dataset="score", requested_date=CURRENT_DATE, requested_start_date=CURRENT_DATE, requested_end_date=CURRENT_DATE, status=status, started_at=CURRENT_TIME, finished_at=CURRENT_TIME, stocks_attempted=stock_count, stocks_completed=stock_count if status == "SUCCESS" else 0, stocks_failed=0 if status == "SUCCESS" else stock_count, checkpoint_state=checkpoint))
        db.commit()


def _assert_existing_scores_are_served(*, provider_blocked: bool | None = None) -> None:
    with TestClient(app) as client:
        summary = client.get("/api/summary").json()
        data_status = client.get("/api/data-status").json()
        stocks = client.get("/api/stocks?page=1&page_size=50&sort=score").json()
        minimum_score_stocks = client.get("/api/stocks?page=1&page_size=50&sort=score&min_score=0").json()
        rankings = client.get("/api/rankings?kind=top&limit=50").json()
        detail = client.get("/api/stocks/2330?limit=20").json()

    if provider_blocked is not None:
        assert summary["provider_state"]["numeric_scores_allowed"] is (not provider_blocked)
    assert summary["latest_score_date"] == CURRENT_DATE.isoformat()
    assert summary["historical_latest_score_date"] == CURRENT_DATE.isoformat()
    assert summary["strong_count"] == 1
    assert summary["accumulation_count"] == 1
    assert summary["watch_count"] == 2
    assert summary["data_insufficient_count"] == 2
    assert summary["score_ready"] is True
    assert data_status["provider_state"]["numeric_scores_allowed"] == summary["provider_state"]["numeric_scores_allowed"]
    assert data_status["latest_score_date"] == CURRENT_DATE.isoformat()
    assert rankings["items"]
    assert all(isinstance(item["score"], (int, float)) for item in rankings["items"])
    listed = {item["stock_id"]: item for item in stocks["items"]}
    assert listed["2330"]["score"] == 88.0
    assert listed["2330"]["status"] == "STRONG_ACCUMULATION"
    assert listed["1103"]["score"] is None
    assert listed["1103"]["status"] == "DATA_INSUFFICIENT"
    assert minimum_score_stocks["total"] == 4
    assert all(item["score"] is not None for item in minimum_score_stocks["items"])
    assert detail["score"]["score"] == 88.0
    assert detail["score"]["status"] == "STRONG_ACCUMULATION"


@pytest.mark.parametrize("error_code", ["QUOTA_EXHAUSTED", "ACCESS_DENIED", "AUTHENTICATION_FAILED", "SCHEMA_MISMATCH"])
def test_provider_failure_suppresses_existing_numeric_scores(error_code: str) -> None:
    _seed_authoritative_sync(failing_dataset="TaiwanStockPrice", status="FAILED", error_code=error_code)
    _assert_existing_scores_are_served(provider_blocked=True)


@pytest.mark.parametrize("status", ["FAILED", "PARTIAL"])
def test_provider_partial_coverage_suppresses_existing_numeric_scores(status: str) -> None:
    _seed_authoritative_sync(failing_dataset="TaiwanStockShareholding", status=status)
    _assert_existing_scores_are_served(provider_blocked=True)


def test_empty_provider_state_is_not_available() -> None:
    _assert_existing_scores_are_served(provider_blocked=True)


def test_complete_authoritative_sync_is_required_before_current_scores_are_served() -> None:
    _seed_authoritative_sync()
    _seed_score_job()
    with TestClient(app) as client:
        summary = client.get("/api/summary").json()
        rankings = client.get("/api/rankings?kind=top&limit=50").json()
        detail = client.get("/api/stocks/2330?limit=20").json()

    assert summary["provider_state"]["numeric_scores_allowed"] is True
    assert rankings["items"]
    assert detail["score"]["score"] == 88.0


def test_existing_scores_remain_visible_when_source_dates_advance() -> None:
    _seed_authoritative_sync(current_date=date(2026, 8, 21))
    _assert_existing_scores_are_served(provider_blocked=False)


def test_failed_current_score_job_does_not_hide_existing_scores() -> None:
    _seed_authoritative_sync()
    _seed_score_job(status="FAILED")
    _assert_existing_scores_are_served(provider_blocked=False)


def test_current_score_formula_or_source_binding_mismatch_does_not_hide_existing_scores() -> None:
    _seed_authoritative_sync()
    _seed_score_job(checkpoint_overrides={"formula_hash": "f" * 64})
    _assert_existing_scores_are_served(provider_blocked=False)


def test_detail_prefers_latest_numeric_score_over_newer_fail_closed_snapshot() -> None:
    _seed_authoritative_sync()
    with SessionLocal() as db:
        existing = db.scalar(select(AccumulationScore).where(AccumulationScore.stock_id == "2330", AccumulationScore.score.is_not(None)).limit(1))
        assert existing is not None
        db.add(AccumulationScore(
            stock_id="2330",
            source_date=date(2026, 8, 21),
            score=None,
            status="DATA_INSUFFICIENT",
            score_version=SCORE_VERSION,
            components={},
            explanation=[{"label": "資料不足", "value": 0, "detail": "newer snapshot"}],
            coverage={},
            calculated_at=CURRENT_TIME,
            knowledge_cutoff=CURRENT_TIME,
            input_snapshot_hash="f" * 64,
            input_source_hashes=[],
            formula_hash=FORMULA_HASH,
        ))
        db.commit()
    with TestClient(app) as client:
        detail = client.get("/api/stocks/2330?limit=20").json()
    assert detail["score"]["score"] == 88.0
    assert detail["score"]["source_date"] == CURRENT_DATE.isoformat()
