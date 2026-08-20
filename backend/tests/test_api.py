from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


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


def test_api_contract_exposes_score_hash_filters_rankings_and_sync_counters() -> None:
    with TestClient(app) as client:
        spec = client.get("/api/score-spec")
        ranking = client.get("/api/rankings", params={"kind": "top", "limit": 10})
        filtered = client.get("/api/stocks", params={"page": 1, "page_size": 10, "status": "DATA_INSUFFICIENT", "min_score": 0})
        data_status = client.get("/api/data-status")
    assert spec.status_code == 200
    assert len(spec.json()["formula_hash"]) == 64
    assert ranking.status_code == 200
    assert ranking.json()["score_version"] == spec.json()["score_version"]
    assert filtered.status_code == 200
    assert all(item["status"] == "DATA_INSUFFICIENT" for item in filtered.json()["items"])
    assert data_status.status_code == 200
    for row in data_status.json()["datasets"]:
        assert {"rows_received_this_attempt", "rows_accepted_this_attempt", "rows_rejected_this_attempt", "stored_rows_total"} <= set(row)
