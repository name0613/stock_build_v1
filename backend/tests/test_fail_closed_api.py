from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import CURRENT_SCORE_DATASETS, app
from app.models import DataSyncStatus


CURRENT_DATE = date(2026, 8, 20)
CURRENT_TIME = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)


def _clear_sync_status() -> None:
    with SessionLocal() as db:
        db.query(DataSyncStatus).delete()
        db.commit()


@pytest.fixture(autouse=True)
def clean_sync_status() -> None:
    _clear_sync_status()
    yield
    _clear_sync_status()


def _seed_authoritative_sync(*, failing_dataset: str | None = None, status: str = "SUCCESS", error_code: str | None = None) -> None:
    with SessionLocal() as db:
        for dataset in CURRENT_SCORE_DATASETS:
            row = DataSyncStatus(
                dataset=dataset,
                status=status if dataset == failing_dataset else "SUCCESS",
                latest_source_date=CURRENT_DATE,
                last_successful_sync=CURRENT_TIME,
                last_http_success_at=CURRENT_TIME,
                last_fully_successful_sync=CURRENT_TIME,
                last_usable_data_at=CURRENT_TIME,
                last_attempt_at=CURRENT_TIME,
                last_fetch_at=CURRENT_TIME,
                usable_records=1,
                stored_records=1,
                staleness_state="ERROR" if dataset == failing_dataset and status == "FAILED" else ("PARTIAL" if dataset == failing_dataset else "FRESH"),
                attempt_latest_source_date=CURRENT_DATE,
                expected_latest_source_date=CURRENT_DATE,
                last_error_code=error_code if dataset == failing_dataset else None,
            )
            db.add(row)
        db.commit()


def _assert_current_score_surfaces_are_blocked() -> None:
    with TestClient(app) as client:
        summary = client.get("/api/summary").json()
        stocks = client.get("/api/stocks?page=1&page_size=50&sort=score&min_score=0").json()
        rankings = client.get("/api/rankings?kind=top&limit=50").json()
        detail = client.get("/api/stocks/2330?limit=20").json()

    assert summary["provider_state"]["numeric_scores_allowed"] is False
    assert summary["latest_score_date"] is None
    assert summary["historical_latest_score_date"] == CURRENT_DATE.isoformat()
    assert summary["strong_count"] == 0
    assert summary["accumulation_count"] == 0
    assert summary["watch_count"] == 0
    assert summary["data_insufficient_count"] == summary["stock_count"]
    assert rankings["items"] == []
    assert all(item["score"] is None and item["status"] == "DATA_INSUFFICIENT" for item in stocks["items"])
    assert detail["score"]["score"] is None
    assert detail["score"]["status"] == "DATA_INSUFFICIENT"


@pytest.mark.parametrize("error_code", ["QUOTA_EXHAUSTED", "ACCESS_DENIED", "AUTHENTICATION_FAILED", "SCHEMA_MISMATCH"])
def test_provider_failure_suppresses_existing_numeric_scores(error_code: str) -> None:
    _seed_authoritative_sync(failing_dataset="TaiwanStockPrice", status="FAILED", error_code=error_code)
    _assert_current_score_surfaces_are_blocked()


@pytest.mark.parametrize("status", ["FAILED", "PARTIAL"])
def test_provider_partial_coverage_suppresses_existing_numeric_scores(status: str) -> None:
    _seed_authoritative_sync(failing_dataset="TaiwanStockShareholding", status=status)
    _assert_current_score_surfaces_are_blocked()


def test_empty_provider_state_is_not_available() -> None:
    _assert_current_score_surfaces_are_blocked()


def test_complete_authoritative_sync_is_required_before_current_scores_are_served() -> None:
    _seed_authoritative_sync()
    with TestClient(app) as client:
        summary = client.get("/api/summary").json()
        rankings = client.get("/api/rankings?kind=top&limit=50").json()
        detail = client.get("/api/stocks/2330?limit=20").json()

    assert summary["provider_state"]["numeric_scores_allowed"] is True
    assert rankings["items"]
    assert detail["score"]["score"] == 88.0
