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
    assert delays == [0.0]
    bad = _Client([_Response(200, ValueError("invalid json"))])
    monkeypatch.setattr(httpx, "Client", lambda **_: bad)
    with pytest.raises(FinMindError) as exc:
        client.fetch("TaiwanStockPrice", data_id="2330", persist_raw=False)
    assert exc.value.code == "SCHEMA_MISMATCH"
    assert bad.calls == 1
