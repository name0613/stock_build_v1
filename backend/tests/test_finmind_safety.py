from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.finmind import CAPABILITY_ONLY_DATASETS, FORBIDDEN_DATASETS, PRODUCTION_S_DATASETS, FinMindClient, FinMindError, capability_evidence, classify_empty_response, expected_observation_dates
from app.ingestion import _mark_sync, ingest_records, normalize_stock
from app.models import Base, DataSyncStatus, InstitutionalDaily, Stock


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
    assert "TaiwanStockTradingDailyReportSecIdAgg" in CAPABILITY_ONLY_DATASETS
    assert "TaiwanStockTradingDailyReportSecIdAgg" not in PRODUCTION_S_DATASETS
    assert "TaiwanStockGovernmentBankBuySell" in FORBIDDEN_DATASETS


def test_capability_only_dataset_is_rejected_by_production_fetch_and_ingestion(tmp_path: Path) -> None:
    client = FinMindClient(Settings(raw_root=tmp_path))
    with pytest.raises(FinMindError) as fetch_error:
        client.fetch("TaiwanStockTradingDailyReportSecIdAgg", data_id="2330")
    assert fetch_error.value.code == "DATASET_NOT_ALLOWLISTED"

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        with pytest.raises(FinMindError) as ingest_error:
            ingest_records(db, "TaiwanStockTradingDailyReportSecIdAgg", [{"stock_id": "2330", "date": "2026-08-20"}])
    assert ingest_error.value.code == "CAPABILITY_ONLY_DATASET"


def test_capability_probe_uses_probe_only_path_without_production_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = FinMindClient(Settings(raw_root=tmp_path))
    calls: list[str] = []

    def probe_fetch(dataset: str, *_args, **_kwargs):
        calls.append(dataset)
        return ([{"stock_id": "2330", "date": "2026-08-20", "securities_trader_id": "075T"}], {"source_date": "2026-08-20"})

    monkeypatch.setattr(client, "_fetch_capability_probe", probe_fetch)
    result = client.probe("TaiwanStockTradingDailyReportSecIdAgg", mode="per_stock", production_used=False)
    assert result.accessible is True
    assert result.production_used is False
    assert calls == ["TaiwanStockTradingDailyReportSecIdAgg"]


def test_capability_evidence_is_bound_to_source_and_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = FinMindClient(Settings(raw_root=tmp_path))
    monkeypatch.setattr(client, "probe", lambda dataset, **kwargs: type("Result", (), {"__dataclass_fields__": {}})())
    monkeypatch.setattr("app.finmind.asdict", lambda _result: {"dataset": "probe", "production_used": False, "classification": "EMPTY_RESPONSE", "query_mode": "per_stock"})
    evidence = capability_evidence(client, source_revision="a" * 40)
    assert evidence["source_revision"] == "a" * 40
    assert evidence["request_policy_version"]
    assert evidence["normalization_policy_version"]
    assert len(evidence["dataset_policy_sha256"]) == 64
    assert len(evidence["provider_policy_sha256"]) == 64
    assert evidence["policy"]["capability_only_datasets"] == ["TaiwanStockTradingDailyReportSecIdAgg"]
    assert evidence["results"]
    assert all(result["probe_only"] is True for result in evidence["results"])
    assert all(result["sanitized_request_mode"] is True for result in evidence["results"])
    assert all(result["secret_values_included"] is False for result in evidence["results"])
    assert evidence["secret_values_included"] is False


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


def test_unverified_empty_broker_response_stops_queue_promptly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncio

    calls: list[str] = []
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, broker_concurrency=1))

    def empty(stock_id: str, *_args, **_kwargs):
        calls.append(stock_id)
        return [], {"attempt": 1}

    monkeypatch.setattr(client, "fetch", empty)
    result = asyncio.run(client.fetch_broker_stocks([str(index) for index in range(100)], "2026-08-20", "2026-08-20"))
    assert result["fatal_code"] == "EMPTY_RESPONSE_UNVERIFIED"
    assert len(calls) == 1


def test_source_checkpoint_resumes_finite_quota_without_repeating_completed_stock(tmp_path: Path) -> None:
    import asyncio

    settings = Settings(raw_root=tmp_path, broker_max_retries=0, source_concurrency=1)
    client = FinMindClient(settings)
    first_calls: list[str] = []
    expected = expected_observation_dates("TaiwanStockPrice", __import__("datetime").date(2026, 8, 1), __import__("datetime").date(2026, 8, 20))

    def first_fetch(_dataset: str, stock_id: str, *_args, **_kwargs):
        first_calls.append(stock_id)
        if stock_id == "2330":
            raise FinMindError("QUOTA_EXHAUSTED", "quota exhausted")
        return ([{"stock_id": stock_id, "date": day.isoformat()} for day in expected], {"attempt": 1})

    client.fetch = first_fetch  # type: ignore[method-assign]
    first = asyncio.run(client.fetch_stocks_dataset(["2317", "2330"], "TaiwanStockPrice", "2026-08-01", "2026-08-20"))
    assert first["fatal_code"] == "QUOTA_EXHAUSTED"
    assert first_calls == ["2317", "2330"]

    second_calls: list[str] = []
    resumed_client = FinMindClient(settings)

    def resumed_fetch(_dataset: str, stock_id: str, *_args, **_kwargs):
        second_calls.append(stock_id)
        return ([{"stock_id": stock_id, "date": day.isoformat()} for day in expected], {"attempt": 1})

    resumed_client.fetch = resumed_fetch  # type: ignore[method-assign]
    second = asyncio.run(resumed_client.fetch_stocks_dataset(["2317", "2330"], "TaiwanStockPrice", "2026-08-01", "2026-08-20"))
    assert second["skipped_checkpoint"] == 1
    assert second_calls == ["2330"]
    assert second["fatal_code"] is None


def test_broker_checkpoint_manifest_rejects_changed_universe_and_corruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import json

    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, broker_concurrency=1))
    monkeypatch.setattr(client, "fetch", lambda *_args, **_kwargs: ([{"stock_id": "2330", "date": "2026-08-20", "securities_trader_id": "A"}], {"attempt": 1}))
    first = asyncio.run(client.fetch_broker_stocks(["2330"], "2026-08-20", "2026-08-20"))
    assert first["success"] == 1
    checkpoint_file = next((tmp_path / "checkpoints").glob("TaiwanStockTradingDailyReport-*.json"))
    checkpoint_file.write_text("{not-json", encoding="utf-8")
    corrupt = asyncio.run(client.fetch_broker_stocks(["2330"], "2026-08-20", "2026-08-20"))
    assert corrupt["checkpoint_state"] == "corrupt_ignored"
    changed = asyncio.run(client.fetch_broker_stocks(["2330", "2317"], "2026-08-20", "2026-08-20"))
    assert changed["checkpoint_state"] == "resumed"
    assert changed["skipped_checkpoint"] == 1
    valid_manifests = []
    for path in (tmp_path / "checkpoints").glob("TaiwanStockTradingDailyReport-*.json"):
        try:
            valid_manifests.append(json.loads(path.read_text(encoding="utf-8"))["manifest"])
        except json.JSONDecodeError:
            continue
    assert valid_manifests
    assert all(manifest["checkpoint_version"] == "2026-08-21-incremental-v4" for manifest in valid_manifests)


def test_source_checkpoint_reuses_covered_history_when_window_moves_forward(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    settings = Settings(raw_root=tmp_path, broker_max_retries=0, source_concurrency=1)
    client = FinMindClient(settings)
    calls: list[tuple[str, str]] = []

    def fetch(_dataset: str, stock_id: str, start: str, end: str, **_kwargs):
        calls.append((stock_id, f"{start}:{end}"))
        return ([{"stock_id": stock_id, "date": end}], {"attempt": 1})

    monkeypatch.setattr(client, "fetch", fetch)
    first = asyncio.run(client.fetch_stocks_dataset(["2330"], "TaiwanStockPrice", "2026-08-01", "2026-08-03"))
    second = asyncio.run(client.fetch_stocks_dataset(["2330"], "TaiwanStockPrice", "2026-08-01", "2026-08-04"))
    assert first["success"] == 1
    assert second["skipped_checkpoint"] == 0
    assert calls == [("2330", "2026-08-03:2026-08-03"), ("2330", "2026-08-04:2026-08-04")]


def test_partial_daily_range_keeps_unreturned_sessions_pending_across_restart(tmp_path: Path) -> None:
    import asyncio

    settings = Settings(raw_root=tmp_path, broker_max_retries=0, source_concurrency=1)
    client = FinMindClient(settings)
    calls: list[tuple[str, str]] = []

    def partial(_dataset: str, stock_id: str, start: str, end: str, **_kwargs):
        calls.append((start, end))
        return ([{"stock_id": stock_id, "date": end}], {"attempt": 1})

    client.fetch = partial  # type: ignore[method-assign]
    first = asyncio.run(client.fetch_stocks_dataset(["2330"], "TaiwanStockPrice", "2026-08-03", "2026-08-05"))
    assert first["success"] == 0
    assert first["retryable_pending"] == 1
    assert first["per_stock"]["2330"]["covered_dates"] == ["2026-08-05"]
    assert first["per_stock"]["2330"]["unresolved_dates"] == ["2026-08-03", "2026-08-04"]

    resumed = FinMindClient(settings)

    def recover(_dataset: str, stock_id: str, start: str, end: str, **_kwargs):
        calls.append((start, end))
        dates = expected_observation_dates("TaiwanStockPrice", __import__("datetime").date.fromisoformat(start), __import__("datetime").date.fromisoformat(end))
        return ([{"stock_id": stock_id, "date": day.isoformat()} for day in dates], {"attempt": 1})

    resumed.fetch = recover  # type: ignore[method-assign]
    second = asyncio.run(resumed.fetch_stocks_dataset(["2330"], "TaiwanStockPrice", "2026-08-03", "2026-08-05"))
    assert second["checkpoint_state"] == "resumed"
    assert second["success"] == 1
    assert second["retryable_pending"] == 0
    assert calls == [("2026-08-03", "2026-08-05"), ("2026-08-03", "2026-08-04")]


def test_weekly_holding_checkpoint_retries_unreturned_publication(tmp_path: Path) -> None:
    import asyncio

    settings = Settings(raw_root=tmp_path, broker_max_retries=0, source_concurrency=1)
    client = FinMindClient(settings)
    calls: list[tuple[str, str]] = []

    def holding_rows(stock_id: str, day: str) -> list[dict[str, object]]:
        return [
            {"stock_id": stock_id, "date": day, "holding_shares_threshold": 400001, "percent": 10, "people": 2, "shares": 500000},
            {"stock_id": stock_id, "date": day, "holding_shares_threshold": 1000001, "percent": 5, "people": 1, "shares": 1500000},
        ]

    def partial(_dataset: str, stock_id: str, start: str, end: str, **_kwargs):
        calls.append((start, end))
        return (holding_rows(stock_id, "2026-08-14"), {"attempt": 1})

    client.fetch = partial  # type: ignore[method-assign]
    first = asyncio.run(client.fetch_stocks_dataset(["2330"], "TaiwanStockHoldingSharesPer", "2026-08-07", "2026-08-14"))
    assert first["success"] == 0
    assert first["per_stock"]["2330"]["covered_dates"] == ["2026-08-14"]
    assert first["per_stock"]["2330"]["unresolved_dates"] == ["2026-08-07"]

    resumed = FinMindClient(settings)
    resumed.fetch = lambda _dataset, stock_id, start, end, **_kwargs: (holding_rows(stock_id, start), {"attempt": 1})  # type: ignore[method-assign]
    second = asyncio.run(resumed.fetch_stocks_dataset(["2330"], "TaiwanStockHoldingSharesPer", "2026-08-07", "2026-08-14"))
    assert second["success"] == 1
    assert second["per_stock"]["2330"]["covered_dates"] == ["2026-08-07", "2026-08-14"]
    assert calls == [("2026-08-07", "2026-08-14")]


def test_explicit_valid_no_data_covers_only_named_observation(tmp_path: Path) -> None:
    import asyncio

    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, source_concurrency=1))
    client.fetch = lambda *_args, **_kwargs: ([], {"attempt": 1, "empty_is_valid": True, "empty_reason": "pre_listing", "empty_observation_dates": ["2026-08-03"]})  # type: ignore[method-assign]
    result = asyncio.run(client.fetch_stocks_dataset(["2330"], "TaiwanStockPrice", "2026-08-03", "2026-08-05"))
    assert result["success"] == 0
    assert result["retryable_pending"] == 1
    assert result["per_stock"]["2330"]["covered_dates"] == ["2026-08-03"]
    assert result["per_stock"]["2330"]["verified_no_data_dates"] == ["2026-08-03"]
    assert result["per_stock"]["2330"]["unresolved_dates"] == ["2026-08-04", "2026-08-05"]


def test_incomplete_pagination_cannot_create_checkpoint_coverage(tmp_path: Path) -> None:
    import asyncio

    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, source_concurrency=1))
    client.fetch = lambda _dataset, stock_id, *_args, **_kwargs: ([{"stock_id": stock_id, "date": day} for day in ("2026-08-03", "2026-08-04", "2026-08-05")], {"attempt": 1, "pagination_complete": False})  # type: ignore[method-assign]
    result = asyncio.run(client.fetch_stocks_dataset(["2330"], "TaiwanStockPrice", "2026-08-03", "2026-08-05"))
    assert result["success"] == 0
    assert result["retryable_pending"] == 1
    assert result["per_stock"]["2330"]["covered_dates"] == []
    assert result["per_stock"]["2330"]["error_code"] == "INCOMPLETE_PROVIDER_COVERAGE"


def test_partial_sync_state_cannot_report_authoritative_freshness() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _mark_sync(db, "TaiwanStockPrice", "PARTIAL", 1, __import__("datetime").date(2026, 8, 20), expected_latest=__import__("datetime").date(2026, 8, 20))
        sync = db.get(DataSyncStatus, "TaiwanStockPrice")
        assert sync is not None
        assert sync.staleness_state == "PARTIAL"
        assert sync.last_fully_successful_sync is None


def test_empty_response_requires_explicit_source_semantics() -> None:
    assert classify_empty_response("TaiwanStockPrice", {"empty_is_valid": True, "empty_reason": "pre_listing"})[0] is True
    assert classify_empty_response("TaiwanStockPrice", {"empty_is_valid": True, "empty_reason": "market_closed"})[0] is True
    assert classify_empty_response("TaiwanStockPrice", {"empty_is_valid": False, "empty_reason": "quota"})[0] is False
    assert classify_empty_response("TaiwanStockPrice", {})[0] is False
