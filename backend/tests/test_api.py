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

