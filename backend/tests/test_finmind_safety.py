from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.finmind import FORBIDDEN_DATASETS, FinMindClient, FinMindError
from app.ingestion import ingest_records, normalize_stock
from app.models import Base, InstitutionalDaily, Stock


def test_forbidden_datasets_are_rejected() -> None:
    client = FinMindClient(Settings(raw_root=Path("data/raw-test")))
    with pytest.raises(FinMindError) as exc:
        client.fetch("TaiwanStockMarginPurchaseShortSale")
    assert exc.value.code == "FORBIDDEN_DATASET"
    assert "token" not in str(exc.value).lower()


def test_allowlist_excludes_unknown_dataset() -> None:
    client = FinMindClient(Settings(raw_root=Path("data/raw-test")))
    with pytest.raises(FinMindError) as exc:
        client.fetch("TaiwanStockPriceTick")
    assert exc.value.code == "FORBIDDEN_DATASET"


def test_capability_dataset_list_is_s_only() -> None:
    assert "TaiwanStockTradingDailyReportSecIdAgg" not in FORBIDDEN_DATASETS
    assert "TaiwanStockGovernmentBankBuySell" in FORBIDDEN_DATASETS


def test_universe_excludes_etf_category_from_industry() -> None:
    assert normalize_stock({"stock_id": "0050", "stock_name": "元大台灣50", "type": "twse", "industry_category": "ETF"}) is None
    assert normalize_stock({"stock_id": "2330", "stock_name": "台積電", "type": "twse", "industry_category": "半導體業"})["is_common_stock"] is True


def test_stock_datasets_ignore_non_common_rows_before_foreign_key_insert() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Stock(stock_id="2330", stock_name="台積電", market="上市", is_common_stock=True))
        db.commit()
        count = ingest_records(
            db,
            "TaiwanStockInstitutionalInvestorsBuySellWide",
            [
                {"stock_id": "2330", "date": "2026-08-20", "Foreign_Investor_buy": 10, "Foreign_Investor_sell": 2},
                {"stock_id": "00400A", "date": "2026-08-20", "Foreign_Investor_buy": 10, "Foreign_Investor_sell": 2},
            ],
        )
        assert count == 1
        assert db.scalar(select(InstitutionalDaily.stock_id)) == "2330"


class _Response:
    def __init__(self, status_code: int = 200, payload: object | None = None, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"data": []}
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    def __init__(self, responses: list[object]):
        self.responses = responses
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, *_args, **_kwargs):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize("response,code", [(_Response(401), "AUTHENTICATION_FAILED"), (_Response(402), "QUOTA_EXHAUSTED"), (_Response(403), "ACCESS_DENIED"), (_Response(400), "NON_RETRYABLE_4XX")])
def test_finmind_non_retryable_and_quota_fail_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response: _Response, code: str) -> None:
    fake = _Client([response])
    monkeypatch.setattr(httpx, "Client", lambda **_: fake)
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=4))
    with pytest.raises(FinMindError) as exc:
        client.fetch("TaiwanStockPrice", data_id="2330", persist_raw=False)
    assert exc.value.code == code
    assert fake.calls == 1
    assert "token" not in str(exc.value).lower()


def test_finmind_retry_after_is_honored_and_schema_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _Client([_Response(429, headers={"retry-after": "0"}), _Response(200, {"data": []})])
    delays: list[float] = []
    monkeypatch.setattr(httpx, "Client", lambda **_: fake)
    monkeypatch.setattr("app.finmind.time.sleep", delays.append)
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=1))
    records, _ = client.fetch("TaiwanStockPrice", data_id="2330", persist_raw=False)
    assert records == []
    assert len(delays) == 2
    assert delays[0] == 0.0
    assert delays[1] >= 0.0
    bad = _Client([_Response(200, ValueError("invalid json"))])
    monkeypatch.setattr(httpx, "Client", lambda **_: bad)
    with pytest.raises(FinMindError) as exc:
        client.fetch("TaiwanStockPrice", data_id="2330", persist_raw=False)
    assert exc.value.code == "SCHEMA_MISMATCH"
    assert bad.calls == 1


@pytest.mark.parametrize("payload,code", [({"status": 402, "msg": "quota exhausted", "data": []}, "QUOTA_EXHAUSTED"), ({"status": 403, "msg": "permission denied", "data": []}, "ACCESS_DENIED"), ({"status": 500, "msg": "unexpected", "data": []}, "SCHEMA_MISMATCH"), ({"status": 200, "data": {"not": "a list"}}, "SCHEMA_MISMATCH")])
def test_http_200_application_errors_and_schema_drift_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict, code: str) -> None:
    fake = _Client([_Response(200, payload)])
    monkeypatch.setattr(httpx, "Client", lambda **_: fake)
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=3))
    with pytest.raises(FinMindError) as exc:
        client.fetch("TaiwanStockPrice", data_id="2330", persist_raw=False)
    assert exc.value.code == code
    assert fake.calls == 1


def test_global_request_budget_covers_retry_attempts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _Client([httpx.TimeoutException("timeout"), _Response(200, {"data": []})])
    monkeypatch.setattr(httpx, "Client", lambda **_: fake)
    waits: list[int] = []
    monkeypatch.setattr("app.finmind.time.sleep", lambda _delay: None)
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=1))
    monkeypatch.setattr(client, "_wait_for_http_attempt", lambda: waits.append(1))
    client.fetch("TaiwanStockPrice", data_id="2330", persist_raw=False)
    assert len(waits) == 2
    assert fake.calls == 2


def test_broker_checkpoint_retains_retryable_failure_and_resumes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncio
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, broker_concurrency=1))
    monkeypatch.setattr(client, "fetch", lambda *_args, **_kwargs: (_ for _ in ()).throw(FinMindError("TIMEOUT", "temporary")))
    failed = asyncio.run(client.fetch_broker_stocks(["2330"], "2026-08-20", "2026-08-20"))
    assert failed["retryable_failed"] == 1
    checkpoint = next((tmp_path / "checkpoints").glob("*.json"))
    failure = __import__("json").loads(checkpoint.read_text(encoding="utf-8"))["failed"][0]
    assert failure["classification"] == "retryable_failed"
    monkeypatch.setattr(client, "fetch", lambda *_args, **_kwargs: ([{"stock_id": "2330", "date": "2026-08-20", "securities_trader_id": "A", "buy": 10, "sell": 1}], {"attempt": 1}))
    resumed = asyncio.run(client.fetch_broker_stocks(["2330"], "2026-08-20", "2026-08-20"))
    assert resumed["success"] == 1
    assert resumed["fatal_code"] is None


def test_global_broker_fatal_stops_queue_promptly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncio
    calls: list[str] = []
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, broker_concurrency=1))
    def denied(stock_id: str, *_args, **_kwargs):
        calls.append(stock_id)
        raise FinMindError("ACCESS_DENIED", "permission denied")
    monkeypatch.setattr(client, "fetch", denied)
    result = asyncio.run(client.fetch_broker_stocks([str(index) for index in range(100)], "2026-08-20", "2026-08-20"))
    assert result["fatal_code"] == "ACCESS_DENIED"
    assert len(calls) == 1
