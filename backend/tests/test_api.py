from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.calendar import CALENDAR_HASH
from app.main import _load_build_metadata, app
from app.scoring import FORMULA_HASH


def test_health_endpoint_has_no_secret_fields() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "token" not in str(payload).lower()
    assert "password" not in str(payload).lower()


def test_stocks_endpoint_supports_pagination_and_safe_sort() -> None:
    with TestClient(app) as client:
        response = client.get("/api/stocks", params={"page": 1, "page_size": 10, "sort": "score"})
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"items", "total", "page", "page_size"}
    assert len(payload["items"]) <= 10


def test_stocks_filters_sort_nulls_and_pagination_return_real_ids() -> None:
    with TestClient(app) as client:
        first = client.get("/api/stocks", params={"page": 1, "page_size": 5, "sort": "score", "order": "desc"}).json()
        second = client.get("/api/stocks", params={"page": 2, "page_size": 5, "sort": "score", "order": "desc"}).json()
        searched = client.get("/api/stocks", params={"page": 1, "page_size": 10, "search": "2330", "market": "上市"}).json()
    items = first["items"]
    for left, right in zip(items, items[1:]):
        if left["score"] is None:
            assert right["score"] is None
            assert left["stock_id"] <= right["stock_id"]
        elif right["score"] is not None:
            assert left["score"] >= right["score"]
            if left["score"] == right["score"]:
                assert left["stock_id"] <= right["stock_id"]
    if first["total"] > 5:
        assert set(item["stock_id"] for item in items).isdisjoint(item["stock_id"] for item in second["items"])
    assert all(item["market"] == "上市" and ("2330" in item["stock_id"] or "2330" in item["stock_name"]) for item in searched["items"])


def test_api_contract_exposes_score_hash_filters_rankings_and_sync_counters() -> None:
    with TestClient(app) as client:
        spec = client.get("/api/score-spec")
        summary = client.get("/api/summary")
        ranking = client.get("/api/rankings", params={"kind": "top", "limit": 10})
        filtered = client.get("/api/stocks", params={"page": 1, "page_size": 10, "status": "DATA_INSUFFICIENT", "min_score": 0})
        data_status = client.get("/api/data-status")
    assert spec.status_code == 200
    assert len(spec.json()["formula_hash"]) == 64
    assert summary.status_code == 200
    assert summary.json()["provider_state"]["score_policy"] in {"S_ONLY_V6", "FAIL_CLOSED"}
    assert summary.json()["provider_state"]["reason_code"] is None or isinstance(summary.json()["provider_state"]["reason_code"], str)
    assert ranking.status_code == 200
    assert ranking.json()["score_version"] == spec.json()["score_version"]
    assert filtered.status_code == 200
    assert all(item["status"] == "DATA_INSUFFICIENT" for item in filtered.json()["items"])
    assert data_status.status_code == 200
    for row in data_status.json()["datasets"]:
        assert {"physical_requests_this_attempt", "rows_received_this_attempt", "rows_accepted_this_attempt", "rows_rejected_this_attempt", "rows_versioned_this_attempt", "observations_reused_this_attempt", "stored_rows_total", "counter_attempt_id", "counter_semantics_version", "counters_are_current_attempt", "historical_pre_v5_counters"} <= set(row)
        if row["counters_are_current_attempt"]:
            assert row["counter_semantics_version"] == "attempt-v5-reconciled-v1"
            assert row["rows_received_this_attempt"] == row["rows_accepted_this_attempt"] + row["rows_rejected_this_attempt"]
            assert row["rows_versioned_this_attempt"] <= row["rows_accepted_this_attempt"]


def test_detail_contract_exposes_provenance_version_reasons_and_null_safe_charts() -> None:
    with TestClient(app) as client:
        stocks = client.get("/api/stocks", params={"page": 1, "page_size": 1}).json()
        assert stocks["items"]
        stock_id = stocks["items"][0]["stock_id"]
        detail = client.get(f"/api/stocks/{stock_id}", params={"limit": 20})
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["stock"]["stock_id"] == stock_id
    assert len(payload["score"]["formula_hash"]) == 64
    assert "coverage" in payload["score"]
    assert "explanation" in payload["score"]
    assert set(payload["holding_series"]) == {"400", "1000"}
    assert all(point["value"] is None or isinstance(point["value"], (int, float)) for points in payload["holding_series"].values() for point in points)


def test_worker_health_contract_is_sanitized() -> None:
    with TestClient(app) as client:
        response = client.get("/api/worker-health")
    assert response.status_code == 200
    assert "token" not in response.text.lower()
    assert "password" not in response.text.lower()


def test_build_metadata_reports_valid_missing_malformed_and_mismatched_states(tmp_path) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"source_revision": "a" * 40, "backend_lock_sha256": "b" * 64, "score_spec_hash": FORMULA_HASH, "calendar_hash": CALENDAR_HASH, "build_timestamp": "2026-08-21T00:00:00+00:00"}), encoding="utf-8")
    assert _load_build_metadata(valid)["build_metadata_available"] is True
    assert _load_build_metadata(tmp_path / "missing.json")["build_metadata_available"] is False
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not-json", encoding="utf-8")
    assert _load_build_metadata(malformed)["error_code"] == "BUILD_METADATA_MISSING_OR_INVALID"
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"source_revision": "a" * 40}), encoding="utf-8")
    assert _load_build_metadata(incomplete)["error_code"] == "BUILD_METADATA_FIELDS_MISSING"
    mismatch = tmp_path / "mismatch.json"
    mismatch.write_text(json.dumps({"source_revision": "a" * 40, "backend_lock_sha256": "b" * 64, "score_spec_hash": "c" * 64, "calendar_hash": CALENDAR_HASH, "build_timestamp": "2026-08-21T00:00:00+00:00"}), encoding="utf-8")
    assert _load_build_metadata(mismatch)["error_code"] == "BUILD_METADATA_PROVENANCE_MISMATCH"
