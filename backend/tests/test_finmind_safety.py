from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.finmind import CAPABILITY_ONLY_DATASETS, FORBIDDEN_DATASETS, PRODUCTION_S_DATASETS, FinMindClient, FinMindError, _broker_report_contract, _record_observation_dates, capability_evidence, classify_empty_response, expected_observation_dates
from app.ingestion import _mark_sync, ingest_records, normalize_stock
from app.models import Base, DataSyncStatus, InstitutionalDaily, Stock
from app.scoring import BROKER_ROW_CONTRACT_VERSION, HOLDING_CANONICAL_LEVELS


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


def test_provider_quota_is_direct_source_bound_and_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _Client([_Response(200, {"data": {"api_request_limit": "6,000", "user_count": 123}})])
    monkeypatch.setattr(httpx, "Client", lambda **_: fake)
    client = FinMindClient(Settings(raw_root=tmp_path, finmind_api_token="test-only-secret"))
    evidence = client.provider_quota(source_revision="a" * 40)
    serialized = __import__("json").dumps(evidence)
    assert evidence["status"] == "PASS"
    assert evidence["direct_provider_response"] is True
    assert evidence["source_revision"] == "a" * 40
    assert evidence["plan"] == "Sponsor"
    assert evidence["provider_reported_limit_per_hour"] == 6_000
    assert evidence["provider_reported_used"] == 123
    assert evidence["provider_reported_remaining"] == 5_877
    assert evidence["raw_response_persisted"] is False
    assert "test-only-secret" not in serialized


@pytest.mark.parametrize("payload", [{"data": {"user_count": 1}}, {"data": {"api_request_limit": 6000}}, {"data": {"api_request_limit": "bad", "user_count": 1}}])
def test_provider_quota_schema_drift_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict) -> None:
    monkeypatch.setattr(httpx, "Client", lambda **_: _Client([_Response(200, payload)]))
    client = FinMindClient(Settings(raw_root=tmp_path, finmind_api_token="test-only-secret"))
    with pytest.raises(FinMindError) as exc:
        client.provider_quota(source_revision="a" * 40)
    assert exc.value.code == "SCHEMA_MISMATCH"
    assert "test-only-secret" not in str(exc.value)


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


def test_broker_provider_contract_validates_rows_without_claiming_report_completeness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    row = {"stock_id": "2330", "date": "2026-08-20", "securities_trader_id": "A", "buy": 10, "sell": 1}
    complete = _Client([_Response(200, {"status": 200, "data": [row]})])
    monkeypatch.setattr(httpx, "Client", lambda **_: complete)
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0))
    records, meta = client.fetch("TaiwanStockTradingDailyReport", "2330", "2026-08-20", "2026-08-20", persist_raw=False)
    assert meta["provider_report_complete"] is False
    assert meta["provider_contract_version"] is None
    assert meta["provider_row_validated"] is True
    assert meta["provider_row_contract_version"] == BROKER_ROW_CONTRACT_VERSION
    assert all(record["provider_report_complete"] is False for record in records)
    assert all(record["provider_row_validated"] is True for record in records)

    incomplete = _Client([_Response(200, {"status": 200, "pagination_complete": False, "data": [row]})])
    monkeypatch.setattr(httpx, "Client", lambda **_: incomplete)
    records, meta = client.fetch("TaiwanStockTradingDailyReport", "2330", "2026-08-20", "2026-08-20", persist_raw=False)
    assert meta["provider_report_complete"] is False
    assert meta["provider_contract_version"] is None
    assert meta["provider_row_validated"] is True
    assert meta["provider_row_contract_version"] == BROKER_ROW_CONTRACT_VERSION
    assert all(record["provider_report_complete"] is False for record in records)


def test_broker_checkpoint_retains_retryable_failure_and_resumes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncio
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, broker_concurrency=1))
    monkeypatch.setattr(client, "fetch", lambda *_args, **_kwargs: (_ for _ in ()).throw(FinMindError("TIMEOUT", "temporary")))
    failed = asyncio.run(client.fetch_broker_stocks(["2330"], "2026-08-20", "2026-08-20"))
    assert failed["retryable_failed"] == 1
    checkpoint = next((tmp_path / "checkpoints").glob("*.json"))
    failure = __import__("json").loads(checkpoint.read_text(encoding="utf-8"))["failed"][0]
    assert failure["classification"] == "retryable_failed"
    monkeypatch.setattr(client, "fetch", lambda *_args, **_kwargs: ([{"stock_id": "2330", "date": "2026-08-20", "securities_trader_id": "A", "buy": 10, "sell": 1, "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION}], {"attempt": 1, "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION}))
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


def test_unverified_empty_broker_response_is_per_stock_retryable_and_does_not_stop_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncio

    calls: list[str] = []
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, broker_concurrency=1))

    def empty(stock_id: str, *_args, **_kwargs):
        calls.append(stock_id)
        return [], {"attempt": 1}

    monkeypatch.setattr(client, "fetch", empty)
    result = asyncio.run(client.fetch_broker_stocks([str(index) for index in range(100)], "2026-08-20", "2026-08-20"))
    assert result["fatal_code"] is None
    assert result["retryable_failed"] == 100
    assert len(calls) == 100
    checkpoint = next((tmp_path / "checkpoints").glob("*.json"))
    failures = __import__("json").loads(checkpoint.read_text(encoding="utf-8"))["failed"]
    assert len(failures) == 100
    assert {failure["code"] for failure in failures} == {"EMPTY_RESPONSE_UNVERIFIED"}
    assert {failure["classification"] for failure in failures} == {"retryable_failed"}


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


def test_source_checkpoint_round_robin_cursor_prevents_later_stock_starvation(tmp_path: Path) -> None:
    import asyncio

    settings = Settings(raw_root=tmp_path, broker_max_retries=0, source_concurrency=1)
    stock_ids = [f"{index:04d}" for index in range(1, 9)]
    expected = expected_observation_dates("TaiwanStockPrice", __import__("datetime").date(2026, 8, 3), __import__("datetime").date(2026, 8, 5))

    def quota_run(allowance: int, calls: list[str]):
        def fetch(_dataset: str, stock_id: str, *_args, **_kwargs):
            calls.append(stock_id)
            if len(calls) > allowance:
                raise FinMindError("QUOTA_EXHAUSTED", "finite test budget")
            return ([{"stock_id": stock_id, "date": day.isoformat()} for day in expected], {"attempt": 1})
        client = FinMindClient(settings)
        client.fetch = fetch  # type: ignore[method-assign]
        return asyncio.run(client.fetch_stocks_dataset(stock_ids, "TaiwanStockPrice", "2026-08-03", "2026-08-05"))

    first_calls: list[str] = []
    first = quota_run(2, first_calls)
    assert first_calls == ["0001", "0002", "0003"]
    assert first["fair_cursor_end_stock_id"] == "0004"
    second_calls: list[str] = []
    second = quota_run(2, second_calls)
    assert second_calls == ["0004", "0005", "0006"]
    assert second["fair_cursor_start_stock_id"] == "0004"
    assert second["fair_cursor_end_stock_id"] == "0007"
    recovery_calls: list[str] = []
    recovered = quota_run(99, recovery_calls)
    assert recovered["fatal_code"] is None
    assert {"0007", "0008", "0003", "0006"} <= set(recovery_calls)
    assert "0001" not in recovery_calls and "0002" not in recovery_calls
    assert recovered["success"] == len(stock_ids)


def test_source_checkpoint_v5_migration_preserves_coverage_and_fair_cursor(tmp_path: Path) -> None:
    import asyncio
    import json

    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    dates = ["2026-08-03", "2026-08-04", "2026-08-05"]
    legacy = {
        "manifest": {"dataset": "TaiwanStockPrice", "checkpoint_version": "2026-08-21-incremental-v5"},
        "manifest_hash": "legacy",
        "completed": ["0001"],
        "no_data_but_valid": [],
        "failed": [],
        "permanent_failed": [],
        "global_fatal": None,
        "entries": {
            "0001": {
                "covered_dates": dates,
                "verified_record_dates": dates,
                "verified_no_data_dates": [],
                "classification": "NEW_SUCCESS",
            }
        },
        "fair_cursor_stock_id": "0002",
    }
    (checkpoint_dir / "source-TaiwanStockPrice-incremental-v5.json").write_text(json.dumps(legacy), encoding="utf-8")
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, source_concurrency=1))
    calls: list[str] = []

    def fetch(_dataset: str, stock_id: str, *_args, **_kwargs):
        calls.append(stock_id)
        return ([{"stock_id": stock_id, "date": day} for day in dates], {"attempt": 1})

    client.fetch = fetch  # type: ignore[method-assign]
    result = asyncio.run(client.fetch_stocks_dataset(["0001", "0002"], "TaiwanStockPrice", dates[0], dates[-1]))
    assert result["checkpoint_state"] == "migrated_v5"
    assert result["fair_cursor_start_stock_id"] == "0002"
    assert result["observations_reused"] == 3
    assert calls == ["0002"]
    assert result["success"] == 2
    assert (checkpoint_dir / "source-TaiwanStockPrice-incremental-v6.json").exists()


def test_source_attempt_counters_reconcile_before_later_quota_failure(tmp_path: Path) -> None:
    import asyncio

    settings = Settings(raw_root=tmp_path, broker_max_retries=0, source_concurrency=1)
    client = FinMindClient(settings)
    expected = expected_observation_dates("TaiwanStockPrice", __import__("datetime").date(2026, 8, 3), __import__("datetime").date(2026, 8, 5))
    calls = 0

    def fetch(_dataset: str, stock_id: str, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise FinMindError("QUOTA_EXHAUSTED", "later quota failure")
        return ([{"stock_id": stock_id, "date": day.isoformat()} for day in expected], {"attempt": 1})

    client.fetch = fetch  # type: ignore[method-assign]
    result = asyncio.run(client.fetch_stocks_dataset(["0001", "0002", "0003"], "TaiwanStockPrice", "2026-08-03", "2026-08-05"))
    assert result["fatal_code"] == "QUOTA_EXHAUSTED"
    assert result["rows_received"] == 6
    assert result["rows_accepted"] == 6
    assert result["rows_rejected"] == 0
    assert result["rows_versioned"] == 6
    assert result["rows_received"] >= result["rows_accepted"] + result["rows_rejected"]


def test_broker_checkpoint_manifest_rejects_changed_universe_and_corruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import json

    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, broker_concurrency=1))
    monkeypatch.setattr(client, "fetch", lambda *_args, **_kwargs: ([{"stock_id": "2330", "date": "2026-08-20", "securities_trader_id": "A", "buy": 1, "sell": 0, "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION}], {"attempt": 1, "provider_row_validated": True, "provider_row_contract_version": BROKER_ROW_CONTRACT_VERSION}))
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
    assert all(manifest["checkpoint_version"] == "2026-08-21-incremental-v5" for manifest in valid_manifests)


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
            {"stock_id": stock_id, "date": day, "HoldingSharesLevel": level, "holding_shares_threshold": threshold, "percent": 1, "people": 1, "shares": threshold}
            for level, threshold in HOLDING_CANONICAL_LEVELS
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


def test_holding_weekly_period_contract_accepts_holiday_shifts_and_rejects_ambiguity() -> None:
    expected = {"2026-08-07", "2026-08-14"}

    def rows(day: str) -> list[dict[str, object]]:
        return [{"stock_id": "2330", "date": day, "HoldingSharesLevel": level, "holding_shares_threshold": threshold, "percent": 1, "people": 1, "shares": threshold} for level, threshold in HOLDING_CANONICAL_LEVELS]

    assert _record_observation_dates("TaiwanStockHoldingSharesPer", "2330", rows("2026-08-07"), expected) == {"2026-08-07"}
    assert _record_observation_dates("TaiwanStockHoldingSharesPer", "2330", rows("2026-08-06"), expected) == {"2026-08-07"}
    assert _record_observation_dates("TaiwanStockHoldingSharesPer", "2330", rows("2026-08-10"), expected) == {"2026-08-07"}
    assert _record_observation_dates("TaiwanStockHoldingSharesPer", "2330", rows("2026-08-02"), {"2026-08-07"}) == set()
    assert _record_observation_dates("TaiwanStockHoldingSharesPer", "2330", rows("2026-08-06") + rows("2026-08-07"), expected) == set()


def test_broker_complete_report_contract_rejects_mixed_missing_null_and_empty_rows() -> None:
    valid = [{"stock_id": "2330", "date": "2026-08-20", "securities_trader_id": "A", "buy": 10, "sell": 1}]
    assert _broker_report_contract(valid, "2330", "2026-08-20") == (True, BROKER_ROW_CONTRACT_VERSION)
    assert _broker_report_contract([], "2330", "2026-08-20") == (False, "empty_report")
    assert _broker_report_contract([{**valid[0], "stock_id": "2317"}], "2330", "2026-08-20") == (False, "mixed_stock_or_session")
    assert _broker_report_contract([{key: value for key, value in valid[0].items() if key != "securities_trader_id"}], "2330", "2026-08-20") == (False, "required_report_field_missing")
    assert _broker_report_contract([{**valid[0], "buy": None}], "2330", "2026-08-20") == (False, "null_buy_or_sell")


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


def test_successful_filtered_empty_source_range_is_checkpointed_as_missing_never_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncio

    fake = _Client([_Response(200, {"status": 200, "data": []})])
    monkeypatch.setattr(httpx, "Client", lambda **_: fake)
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0, source_concurrency=1))
    records, meta = client.fetch("TaiwanStockPrice", "2330", "2026-08-03", "2026-08-05", persist_raw=False)
    assert records == []
    assert meta["provider_http_status"] == 200
    assert meta["provider_application_status"] == 200
    assert meta["empty_is_valid"] is True
    assert meta["empty_reason"] == "no_provider_observation"
    assert meta["empty_observation_dates"] == ["2026-08-03", "2026-08-04", "2026-08-05"]
    assert "never scored as zero" in meta["missing_value_semantics"]

    result = asyncio.run(client.fetch_stocks_dataset(["2330"], "TaiwanStockPrice", "2026-08-03", "2026-08-05"))
    assert result["success"] == 1
    assert result["retryable_pending"] == 0
    assert result["verified_observations"] == 3
    assert result["provider_missing_observations"] == 3
    assert result["unresolved_observations"] == 0
    assert result["rows_received"] == result["rows_accepted"] == result["rows_versioned"] == 0
    assert result["missing_values_imputed_as_zero"] == 0
    assert result["per_stock"]["2330"]["verified_missing_dates"] == ["2026-08-03", "2026-08-04", "2026-08-05"]
    assert result["per_stock"]["2330"]["provider_missing_is_not_zero"] is True


def test_broker_empty_response_is_not_promoted_by_filtered_empty_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _Client([_Response(200, {"status": 200, "data": []})])
    monkeypatch.setattr(httpx, "Client", lambda **_: fake)
    client = FinMindClient(Settings(raw_root=tmp_path, broker_max_retries=0))
    records, meta = client.fetch("TaiwanStockTradingDailyReport", "2330", "2026-08-20", "2026-08-20", persist_raw=False)
    assert records == []
    assert meta["empty_is_valid"] is False
    assert meta["empty_observation_dates"] == []
    assert meta["provider_row_validated"] is False


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
