from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - production image installs pyarrow
    pa = None
    pq = None

from .config import Settings, get_settings
from .calendar import is_trading_session
from .scoring import is_holding_metadata_level, parse_holding_level

logger = logging.getLogger(__name__)
CHECKPOINT_SCHEMA_VERSION = "2026-08-21-v3-observation-bound"
INCREMENTAL_CHECKPOINT_VERSION = "2026-08-21-incremental-v4"
NORMALIZATION_POLICY_VERSION = "s-only-normalization-v4-probe-isolated"
REQUEST_POLICY_VERSION = "finmind-request-policy-v4-observation-coverage"
GLOBAL_PROVIDER_FAILURE_CODES = frozenset({
    "AUTHENTICATION_FAILED",
    "ACCESS_DENIED",
    "QUOTA_EXHAUSTED",
    "SCHEMA_MISMATCH",
    "EMPTY_RESPONSE_UNVERIFIED",
})


def _date_range(start: date, end: date) -> list[date]:
    """Return an inclusive calendar-date range for policy calculations."""
    if end < start:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


DAILY_OBSERVATION_DATASETS = frozenset({
    "TaiwanStockInstitutionalInvestorsBuySellWide",
    "TaiwanStockShareholding",
    "TaiwanStockPrice",
})
WEEKLY_OBSERVATION_DATASETS = frozenset({"TaiwanStockHoldingSharesPer"})


def expected_observation_dates(dataset: str, start: date, end: date) -> list[date]:
    """Return the source-specific observations that a checkpoint must prove."""
    if dataset in DAILY_OBSERVATION_DATASETS:
        return [day for day in _date_range(start, end) if is_trading_session(day)]
    if dataset in WEEKLY_OBSERVATION_DATASETS:
        return [day for day in _date_range(start, end) if day.weekday() == 4]
    raise FinMindError("UNSUPPORTED_OBSERVATION_CONTRACT", f"no checkpoint observation contract for {dataset}")


def _validated_no_data_dates(dataset: str, meta: dict[str, Any], expected: set[str]) -> tuple[set[str], str | None]:
    valid_empty, reason = classify_empty_response(dataset, meta)
    if not valid_empty:
        return set(), None
    raw_dates = meta.get("empty_observation_dates")
    if not isinstance(raw_dates, list):
        return set(), None
    dates = {str(value)[:10] for value in raw_dates if str(value)[:10] in expected}
    return dates, reason if dates else None


def _record_observation_dates(dataset: str, stock_id: str, records: list[dict[str, Any]], expected: set[str]) -> set[str]:
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        row_stock = str(row.get("stock_id") or "").strip()
        source_day = str(row.get("date") or row.get("source_date") or "")[:10]
        if row_stock == stock_id and source_day in expected:
            by_date.setdefault(source_day, []).append(row)
    if dataset != "TaiwanStockHoldingSharesPer":
        return set(by_date)

    verified: set[str] = set()
    for source_day, rows in by_date.items():
        thresholds: dict[int, list[dict[str, Any]]] = {}
        invalid_schema = False
        for row in rows:
            level = row.get("HoldingSharesLevel") or row.get("holding_shares_level")
            if is_holding_metadata_level(level):
                continue
            threshold = row.get("holding_shares_threshold")
            if threshold is None:
                threshold = parse_holding_level(level)
            if threshold is None:
                invalid_schema = True
                continue
            thresholds.setdefault(int(threshold), []).append(row)
        relevant = {threshold: values for threshold, values in thresholds.items() if threshold >= 400_000}
        boundary_400 = any(400_000 <= threshold < 1_000_000 for threshold in relevant)
        boundary_1000 = any(threshold >= 1_000_000 for threshold in relevant)
        complete = (
            not invalid_schema
            and boundary_400
            and boundary_1000
            and all(len(values) == 1 for values in relevant.values())
            and all(all(row.get(field) is not None for field in ("percent", "people", "shares")) for values in relevant.values() for row in values)
        )
        if complete:
            verified.add(source_day)
    return verified


def classify_empty_response(dataset: str, meta: dict[str, Any]) -> tuple[bool, str]:
    """Only accept empty data when a source-specific semantic reason is explicit."""
    reason = str(meta.get("empty_reason") or "").strip().lower()
    valid_reasons = {"pre_listing", "market_closed", "no_provider_observation"}
    if meta.get("empty_is_valid") is True and reason in valid_reasons:
        return True, f"{dataset}:{reason}"
    return False, f"{dataset}:empty_response_semantics_unverified"
# httpx's INFO request logger includes the complete URL.  FinMind carries the
# token as a query parameter for compatibility, so request URLs must never be
# emitted by application or worker logs.
for _http_logger_name in ("httpx", "httpcore"):
    logging.getLogger(_http_logger_name).setLevel(logging.WARNING)

PRODUCTION_S_DATASETS = frozenset({
    "TaiwanStockInstitutionalInvestorsBuySellWide",
    "TaiwanStockShareholding",
    "TaiwanStockHoldingSharesPer",
    "TaiwanStockTradingDailyReport",
})
CAPABILITY_ONLY_DATASETS = frozenset({"TaiwanStockTradingDailyReportSecIdAgg"})
REFERENCE_DATASETS = frozenset({"TaiwanStockInfo", "TaiwanStockPrice", "TaiwanSecuritiesTraderInfo"})
# Backward-compatible name used by evidence/tests. It now means production S
# data only and intentionally excludes every capability-probe-only dataset.
ALLOWED_S_DATASETS = PRODUCTION_S_DATASETS
FORBIDDEN_DATASETS = {
    "TaiwanStockBlockTradingDailyReport", "TaiwanStockBlockTrade", "TaiwanStockActiveETFHolding",
    "TaiwanStockActiveETFHoldingChange", "TaiwanStockMarginPurchaseShortSale", "TaiwanStockMarginMaintenance",
    "TaiwanStockSecuritiesLending", "TaiwanDailyShortSaleBalances", "TaiwanStockGovernmentBankBuySell",
    "TaiwanStockIndustryChainMoneyFlow", "TaiwanStockPriceTick",
}


class FinMindError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


class SchemaMismatch(FinMindError):
    pass


@dataclass(frozen=True)
class CapabilityResult:
    dataset: str
    accessible: bool
    method: str
    latest_date: str | None
    sample_row_count: int
    permission_error: str | None
    status_code: int | None = None
    response_fields: list[str] | None = None
    requested_shape: dict[str, Any] | None = None
    production_used: bool = False
    query_mode: str = "unknown"
    data_id_required: bool = False
    returned_range: dict[str, str | None] | None = None
    classification: str = "UNKNOWN"
    quota_plan: str = "not exposed by provider response"


class RawEvidenceStore:
    def __init__(self, root: Path):
        self.root = root

    def write(self, dataset: str, records: list[dict[str, Any]], parameters: dict[str, Any], source_date: str | None) -> dict[str, Any]:
        if pa is None or pq is None:
            raise FinMindError("RAW_STORAGE_UNAVAILABLE", "Parquet runtime is not installed")
        fetched_at = datetime.now(timezone.utc)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            row_date = str(record.get("date") or record.get("Date") or source_date or "unknown")
            grouped.setdefault(row_date, []).append(record)
        if not grouped:
            grouped = {source_date or "unknown": []}
        paths: list[str] = []
        hashes: list[str] = []
        for row_date, date_records in grouped.items():
            target_dir = self.root / dataset / f"date={row_date}"
            target_dir.mkdir(parents=True, exist_ok=True)
            payload = []
            for record in date_records:
                enriched = dict(record)
                enriched["_evidence_source"] = "FinMind"
                enriched["_evidence_dataset"] = dataset
                enriched["_evidence_source_date"] = row_date
                enriched["_evidence_fetched_at"] = fetched_at.isoformat()
                payload.append(enriched)
            path = target_dir / f"part-{fetched_at.strftime('%Y%m%dT%H%M%S%fZ')}.parquet"
            table = pa.Table.from_pylist(payload or [{"_evidence_dataset": dataset, "_evidence_source_date": row_date, "_evidence_fetched_at": fetched_at.isoformat()}])
            pq.write_table(table, path, compression="zstd")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            path.with_suffix(".metadata.json").write_text(json.dumps({"source": "FinMind", "dataset": dataset, "parameters": parameters, "source_date": row_date, "fetched_at": fetched_at.isoformat(), "sha256": digest}, ensure_ascii=False, indent=2), encoding="utf-8")
            paths.append(str(path))
            hashes.append(digest)
        return {"paths": paths, "sha256": hashes, "fetched_at": fetched_at.isoformat(), "records": len(records)}


class RateLimiter:
    def __init__(self, rate_per_second: float):
        self.interval = 1 / max(rate_per_second, 0.1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self.interval - (now - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class FinMindClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.store = RawEvidenceStore(self.settings.raw_root)
        self.timeout = httpx.Timeout(30.0, connect=10.0)
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0
        # One explicit provider-wide physical-attempt budget covers source,
        # broker, first attempts and retries.  It must not use the faster of
        # two path-specific settings and accidentally exceed the stricter
        # provider-safe limit.
        self._request_interval = 1 / max(self.settings.provider_rate_per_second, 0.1)

    def _wait_for_http_attempt(self) -> None:
        """Apply one process-wide budget to every physical HTTP attempt."""
        with self._request_lock:
            now = time.monotonic()
            delay = self._next_request_at - now
            if delay > 0:
                time.sleep(delay)
            self._next_request_at = time.monotonic() + self._request_interval

    def _request_spec(self, dataset: str, data_id: str | None, start_date: str | None, end_date: str | None, securities_trader_id: str | None = None) -> tuple[str, dict[str, str]]:
        if dataset == "TaiwanStockTradingDailyReport":
            params = {"data_id": data_id or "", "date": end_date or start_date or "", "must_need_date": "true"}
            endpoint = "/taiwan_stock_trading_daily_report"
        elif dataset == "TaiwanStockTradingDailyReportSecIdAgg":
            params = {"data_id": data_id or "", "securities_trader_id": securities_trader_id or "", "start_date": start_date or "", "end_date": end_date or "", "must_need_date": "true"}
            endpoint = "/taiwan_stock_trading_daily_report_secid_agg"
        else:
            params = {"dataset": dataset}
            endpoint = "/data"
            if data_id:
                params["data_id"] = data_id
            if start_date:
                params["start_date"] = start_date
            if end_date:
                params["end_date"] = end_date
        if self.settings.finmind_api_token:
            params["token"] = self.settings.finmind_api_token
        return endpoint, params

    def fetch(self, dataset: str, data_id: str | None = None, start_date: str | None = None, end_date: str | None = None, *, persist_raw: bool = True, securities_trader_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Production fetch path; capability-only datasets are rejected."""
        return self._fetch_provider(dataset, data_id, start_date, end_date, persist_raw=persist_raw, securities_trader_id=securities_trader_id, capability_probe=False)

    def _fetch_capability_probe(self, dataset: str, data_id: str | None = None, start_date: str | None = None, end_date: str | None = None, *, securities_trader_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Probe-only provider path; it never invokes production ingestion."""
        return self._fetch_provider(dataset, data_id, start_date, end_date, persist_raw=True, securities_trader_id=securities_trader_id, capability_probe=True)

    def _fetch_provider(self, dataset: str, data_id: str | None, start_date: str | None, end_date: str | None, *, persist_raw: bool, securities_trader_id: str | None, capability_probe: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if dataset in FORBIDDEN_DATASETS:
            raise FinMindError("FORBIDDEN_DATASET", f"dataset is excluded by S-only policy: {dataset}")
        allowed = PRODUCTION_S_DATASETS | REFERENCE_DATASETS
        if capability_probe:
            allowed |= CAPABILITY_ONLY_DATASETS
        if dataset not in allowed:
            raise FinMindError("DATASET_NOT_ALLOWLISTED", f"dataset is not allowlisted: {dataset}")
        endpoint, params = self._request_spec(dataset, data_id, start_date, end_date, securities_trader_id)
        safe_params = {key: value for key, value in params.items() if key != "token"}
        last_error: FinMindError | None = None
        for attempt in range(self.settings.broker_max_retries + 1):
            try:
                self._wait_for_http_attempt()
                with httpx.Client(base_url=self.settings.finmind_base_url, timeout=self.timeout, follow_redirects=True) as client:
                    response = client.get(endpoint, params=params)
                if response.status_code == 401:
                    raise FinMindError("AUTHENTICATION_FAILED", "FinMind authentication failed", 401)
                if response.status_code == 403:
                    raise FinMindError("ACCESS_DENIED", "FinMind plan permission denied", 403)
                if response.status_code == 402:
                    raise FinMindError("QUOTA_EXHAUSTED", "FinMind quota exhausted; request deferred", 402)
                if response.status_code == 429:
                    retry_after = response.headers.get("retry-after")
                    delay = _parse_retry_after(retry_after)
                    raise FinMindError("RATE_LIMITED", "FinMind rate limit; Retry-After will be honored", 429, delay)
                if response.status_code >= 500:
                    raise FinMindError("UPSTREAM_5XX", "FinMind upstream server error", response.status_code)
                if response.status_code >= 400:
                    raise FinMindError("NON_RETRYABLE_4XX", "FinMind rejected the sanitized request", response.status_code)
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SchemaMismatch("SCHEMA_MISMATCH", "FinMind response was not valid JSON", response.status_code) from exc
                self._validate_application_response(payload, response.status_code)
                records = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(records, list):
                    raise SchemaMismatch("SCHEMA_MISMATCH", "FinMind response data field is not a list", response.status_code)
                normalized = [self._normalize_record(dataset, record) for record in records]
                if dataset == "TaiwanStockTradingDailyReport":
                    # A per-stock report is complete only when the provider
                    # returned a successful report list. Legacy rows without
                    # this marker remain unavailable during scoring.
                    normalized = [{**record, "provider_report_complete": True} for record in normalized]
                source_date = self._latest_date(normalized)
                evidence = self.store.write(dataset, normalized, safe_params, source_date) if persist_raw else {"records": len(normalized)}
                pagination_complete = payload.get("pagination_complete") if isinstance(payload.get("pagination_complete"), bool) else None
                return normalized, {"dataset": dataset, "parameters": safe_params, "source_date": source_date, "evidence": evidence, "attempt": attempt + 1, "pagination_complete": pagination_complete, "probe_only": capability_probe}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = FinMindError("TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "NETWORK_ERROR", "FinMind network request failed")
            except FinMindError as exc:
                last_error = exc
                if exc.code in {"AUTHENTICATION_FAILED", "ACCESS_DENIED", "QUOTA_EXHAUSTED", "SCHEMA_MISMATCH", "NON_RETRYABLE_4XX"}:
                    raise
            if attempt < self.settings.broker_max_retries:
                delay = last_error.retry_after if last_error and last_error.retry_after is not None else min(30, 2 ** attempt + random.random())
                time.sleep(max(0.0, min(60.0, delay)))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _validate_application_response(payload: Any, status_code: int) -> None:
        if not isinstance(payload, dict):
            raise SchemaMismatch("SCHEMA_MISMATCH", "FinMind response root was not an object", status_code)
        status = payload.get("status", payload.get("code"))
        message = str(payload.get("msg") or payload.get("message") or "")
        status_text = str(status).lower() if status is not None else ""
        message_text = message.lower()
        if status_text not in {"", "200", "ok", "success", "true"}:
            if any(term in f"{status_text} {message_text}" for term in ("quota", "limit", "402")):
                raise FinMindError("QUOTA_EXHAUSTED", "FinMind application quota failure", status_code)
            if any(term in f"{status_text} {message_text}" for term in ("permission", "forbidden", "access", "403")):
                raise FinMindError("ACCESS_DENIED", "FinMind application permission failure", status_code)
            if any(term in f"{status_text} {message_text}" for term in ("auth", "token", "401", "unauthorized")):
                raise FinMindError("AUTHENTICATION_FAILED", "FinMind application authentication failure", status_code)
            raise SchemaMismatch("SCHEMA_MISMATCH", "FinMind application returned an unsupported status", status_code)
        if any(term in message_text for term in ("quota exhausted", "rate limit", "limit exceeded", "permission denied")):
            code = "QUOTA_EXHAUSTED" if "quota" in message_text or "limit" in message_text else "ACCESS_DENIED"
            raise FinMindError(code, "FinMind application error", status_code)

    def probe(self, dataset: str, *, mode: str = "per_stock", production_used: bool | None = None) -> CapabilityResult:
        try:
            end_date = date.today().isoformat()
            start_date = (date.today() - timedelta(days=30)).isoformat()
            if dataset in {"TaiwanStockTradingDailyReport", "TaiwanStockTradingDailyReportSecIdAgg"}:
                end_date = (date.today() - timedelta(days=1)).isoformat()
                start_date = end_date
            data_id = None if dataset == "TaiwanStockInfo" or mode == "broad" else "2330"
            trader_id = "075T" if dataset == "TaiwanStockTradingDailyReportSecIdAgg" else None
            records, meta = self._fetch_capability_probe(dataset, data_id=data_id, start_date=None if dataset == "TaiwanStockInfo" else start_date, end_date=None if dataset == "TaiwanStockInfo" else end_date, securities_trader_id=trader_id)
            method = "GET /api/v4/data" if dataset not in {"TaiwanStockTradingDailyReport", "TaiwanStockTradingDailyReportSecIdAgg"} else f"GET /api/v4/{'taiwan_stock_trading_daily_report' if dataset.endswith('Report') else 'taiwan_stock_trading_daily_report_secid_agg'}"
            fields = sorted({key for row in records[:3] for key in row})
            dates = sorted({str(row.get("date") or row.get("source_date"))[:10] for row in records if row.get("date") or row.get("source_date")})
            approved = production_used if production_used is not None else mode == "per_stock" and dataset != "TaiwanStockTradingDailyReportSecIdAgg"
            classification = "PER_STOCK_HISTORY_USABLE" if records and mode == "per_stock" else ("FULL_MARKET_LIMITED_RANGE" if records else "EMPTY_RESPONSE")
            return CapabilityResult(dataset, bool(records), method, meta.get("source_date"), len(records), None, 200, fields, {"data_id": data_id, "start_date": start_date, "end_date": end_date, "securities_trader_id": trader_id, "mode": mode}, approved, mode, dataset != "TaiwanStockInfo", {"start": dates[0] if dates else None, "end": dates[-1] if dates else None}, classification)
        except FinMindError as exc:
            return CapabilityResult(dataset, False, "GET /api/v4/data", None, 0, exc.code, exc.status_code, [], {"data_id": data_id, "start_date": start_date, "end_date": end_date, "securities_trader_id": trader_id, "mode": mode}, False, mode, dataset != "TaiwanStockInfo", {"start": None, "end": None}, exc.code)

    def _normalize_record(self, dataset: str, record: Any) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise SchemaMismatch("SCHEMA_MISMATCH", f"{dataset} returned a non-object row")
        result = {str(k): v for k, v in record.items()}
        if dataset == "TaiwanStockHoldingSharesPer":
            level = result.get("HoldingSharesLevel") or result.get("holding_shares_level")
            result["holding_shares_threshold"] = parse_holding_level(level)
            if level is not None and result["holding_shares_threshold"] is None and not is_holding_metadata_level(level):
                result["_schema_warning"] = "UNRECOGNIZED_HOLDING_LEVEL"
            result["shares"] = result.get("shares") or result.get("HoldingShares") or result.get("unit")
        return result

    @staticmethod
    def _latest_date(records: list[dict[str, Any]]) -> str | None:
        dates = [str(row.get("date") or row.get("Date") or row.get("source_date")) for row in records if row.get("date") or row.get("Date") or row.get("source_date")]
        return max(dates) if dates else None

    async def fetch_broker_stocks(self, stock_ids: list[str], start_date: str, end_date: str, dataset: str = "TaiwanStockTradingDailyReport", *, record_sink: Callable[[list[dict[str, Any]]], int] | None = None, progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Bounded async Sponsor-compatible path with checkpoint/resume semantics."""
        checkpoint_dir = self.settings.raw_root / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        days = [(start_date if start_date == end_date else start_date)]
        if dataset == "TaiwanStockTradingDailyReport":
            from .calendar import expected_trading_sessions
            days = [day.isoformat() for day in expected_trading_sessions(date.fromisoformat(end_date), 20) if date.fromisoformat(start_date) <= day <= date.fromisoformat(end_date)]

        requested_keys = {f"{stock_id}:{requested_date}" for stock_id in stock_ids for requested_date in days}
        universe_hash = hashlib.sha256(json.dumps(sorted(set(stock_ids)), separators=(",", ":")).encode()).hexdigest()
        session_hash = hashlib.sha256(json.dumps(days, separators=(",", ":")).encode()).hexdigest()
        manifest = {"dataset": dataset, "checkpoint_version": INCREMENTAL_CHECKPOINT_VERSION, "schema_version": CHECKPOINT_SCHEMA_VERSION, "normalization_version": NORMALIZATION_POLICY_VERSION, "request_policy_version": REQUEST_POLICY_VERSION, "query_mode": "per_stock_per_session"}
        manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        checkpoint_file = checkpoint_dir / f"{dataset}-incremental-v4.json"
        checkpoint: dict[str, Any] = {"manifest": manifest, "manifest_hash": manifest_hash, "completed": [], "failed": [], "permanent_failed": [], "last_request": {}}
        checkpoint_state = "new"
        if checkpoint_file.exists():
            try:
                candidate = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                if candidate.get("manifest") == manifest and candidate.get("manifest_hash") == manifest_hash:
                    checkpoint = candidate
                    checkpoint_state = "resumed"
                else:
                    checkpoint_state = "incompatible_ignored"
            except (OSError, ValueError, TypeError):
                checkpoint_state = "corrupt_ignored"
        completed = set(checkpoint.get("completed", [])) & requested_keys
        permanent_failed = set(checkpoint.get("permanent_failed", [])) & requested_keys
        semaphore = asyncio.Semaphore(self.settings.broker_concurrency)
        checkpoint_lock = asyncio.Lock()
        sink_lock = asyncio.Lock()
        fatal_event = asyncio.Event()
        metrics = {"requested": len(stock_ids), "requested_keys": len(requested_keys), "skipped_checkpoint": len(completed), "reused_complete": len(completed), "reused_valid_no_data": 0, "newly_fetched": 0, "physical_requests": 0, "checkpoint_state": checkpoint_state, "checkpoint_manifest_hash": manifest_hash, "requested_start_date": start_date, "requested_end_date": end_date, "session_set_hash": session_hash, "universe_hash": universe_hash, "selection_policy": "date_major_round_robin", "success": len(completed), "failed": 0, "stocks_completed": 0, "stocks_failed": 0, "retryable_failed": 0, "permanent_failed": len(permanent_failed), "rows": 0, "retries": 0, "fatal_code": None}
        completed_stocks: set[str] = set()
        failed_stocks: set[str] = set()

        async def persist() -> None:
            temporary = checkpoint_file.with_suffix(".tmp")
            checkpoint["manifest"] = manifest
            checkpoint["manifest_hash"] = manifest_hash
            checkpoint["last_request"] = {"start_date": start_date, "end_date": end_date, "session_set_hash": session_hash, "universe_hash": universe_hash}
            temporary.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(checkpoint_file)

        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=max(1, self.settings.broker_concurrency * 2))

        async def one(stock_id: str, requested_date: str) -> None:
            checkpoint_key = f"{stock_id}:{requested_date}"
            if checkpoint_key in completed or checkpoint_key in permanent_failed or fatal_event.is_set():
                return
            async with semaphore:
                try:
                    metrics["physical_requests"] += 1
                    records, meta = await asyncio.to_thread(self.fetch, dataset, stock_id, requested_date, requested_date)
                    if not records:
                        valid_empty, empty_reason = classify_empty_response(dataset, meta)
                        if not valid_empty:
                            raise FinMindError("EMPTY_RESPONSE_UNVERIFIED", empty_reason)
                    async with checkpoint_lock:
                        if checkpoint_key not in completed:
                            checkpoint["completed"].append(checkpoint_key)
                        completed.add(checkpoint_key)
                        metrics["success"] += 1
                        metrics["newly_fetched"] += 1
                        completed_stocks.add(stock_id)
                        metrics["stocks_completed"] = len(completed_stocks)
                        metrics["rows"] += len(records)
                        metrics["retries"] += max(0, int(meta.get("attempt", 1)) - 1)
                        if records and record_sink:
                            # The provider calls remain concurrent, but the
                            # shared SQLAlchemy sink must be serialized.
                            async with sink_lock:
                                record_sink(records)
                        await persist()
                except FinMindError as exc:
                    async with checkpoint_lock:
                        global_fatal = exc.code in GLOBAL_PROVIDER_FAILURE_CODES
                        permanent = exc.code in {"NON_RETRYABLE_4XX"}
                        retryable = not (global_fatal or permanent)
                        failed_by_key = {item.get("key"): item for item in checkpoint.setdefault("failed", [])}
                        previous = failed_by_key.get(checkpoint_key, {})
                        failure = {"key": checkpoint_key, "stock_id": stock_id, "requested_date": requested_date, "code": exc.code, "classification": "global_fatal" if global_fatal else ("permanent_failed" if permanent else "retryable_failed"), "retryable": retryable, "retry_count": int(previous.get("retry_count", 0)) + 1, "last_attempt_at": datetime.now(timezone.utc).isoformat(), "next_eligible_retry_at": datetime.now(timezone.utc).isoformat() if retryable else None}
                        checkpoint["failed"] = [item for item in checkpoint["failed"] if item.get("key") != checkpoint_key] + [failure]
                        if permanent:
                            checkpoint.setdefault("permanent_failed", []).append(checkpoint_key)
                            permanent_failed.add(checkpoint_key)
                            metrics["permanent_failed"] += 1
                        elif not global_fatal:
                            metrics["retryable_failed"] += 1
                        else:
                            metrics["fatal_code"] = exc.code
                            fatal_event.set()
                        metrics["failed"] += 1
                        failed_stocks.add(stock_id)
                        metrics["stocks_failed"] = len(failed_stocks)
                        await persist()
                finally:
                    if progress_callback:
                        progress_callback(f"{dataset} completed={metrics['stocks_completed']} failed={metrics['stocks_failed']}")

        async def worker() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    await one(*item)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(max(1, self.settings.broker_concurrency))]
        # Date-major ordering gives each stock a fair opportunity in every
        # newly opened trading session instead of draining one stock's full
        # rolling window before starting the next stock.
        for requested_date in days:
            for stock_id in stock_ids:
                await queue.put((stock_id, requested_date))
        await queue.join()
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)
        metrics["retryable_pending"] = sum(1 for item in checkpoint.get("failed", []) if item.get("key") in requested_keys and item.get("classification") == "retryable_failed")
        metrics["permanent_failed"] = len(set(checkpoint.get("permanent_failed", [])) & requested_keys)
        return metrics

    async def fetch_stocks_dataset(self, stock_ids: list[str], dataset: str, start_date: str, end_date: str, *, record_sink: Callable[[list[dict[str, Any]]], int | dict[str, Any]] | None = None, progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Fetch per-stock history with observation-verified checkpoint coverage."""
        stock_ids = sorted(set(stock_ids))
        universe_hash = hashlib.sha256(json.dumps(stock_ids, separators=(",", ":")).encode()).hexdigest()
        expected_days = expected_observation_dates(dataset, date.fromisoformat(start_date), date.fromisoformat(end_date))
        expected_strings = [day.isoformat() for day in expected_days]
        requested_observations = set(expected_strings)
        cadence = "weekly_publication" if dataset in WEEKLY_OBSERVATION_DATASETS else "trading_session"
        manifest = {
            "dataset": dataset,
            "checkpoint_version": INCREMENTAL_CHECKPOINT_VERSION,
            "query_mode": "per_stock_date_range",
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "normalization_version": NORMALIZATION_POLICY_VERSION,
            "request_policy_version": REQUEST_POLICY_VERSION,
            "observation_cadence": cadence,
        }
        manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        checkpoint_dir = self.settings.raw_root / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_file = checkpoint_dir / f"source-{dataset}-incremental-v4.json"
        checkpoint: dict[str, Any] = {"manifest": manifest, "manifest_hash": manifest_hash, "completed": [], "no_data_but_valid": [], "failed": [], "permanent_failed": [], "global_fatal": None, "entries": {}, "last_request": {}}
        checkpoint_state = "new"
        if checkpoint_file.exists():
            try:
                candidate = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                if candidate.get("manifest") == manifest and candidate.get("manifest_hash") == manifest_hash:
                    checkpoint = candidate
                    checkpoint_state = "resumed"
                else:
                    checkpoint_state = "incompatible_ignored"
            except (OSError, ValueError, TypeError):
                checkpoint_state = "corrupt_ignored"

        entries = checkpoint.setdefault("entries", {})
        pending: list[str] = []
        request_ranges: dict[str, tuple[str, str]] = {}
        initial_complete: set[str] = set()
        for stock_id in stock_ids:
            covered = set(entries.get(stock_id, {}).get("covered_dates", [])) & requested_observations
            missing = [day for day in expected_strings if day not in covered]
            if not missing:
                initial_complete.add(stock_id)
                continue
            pending.append(stock_id)
            first_index = expected_strings.index(missing[0])
            block = [missing[0]]
            for source_day in expected_strings[first_index + 1:]:
                if source_day in covered:
                    break
                block.append(source_day)
            request_ranges[stock_id] = (block[0], block[-1])

        previous_global_fatal = checkpoint.get("global_fatal")
        checkpoint["last_global_fatal"] = previous_global_fatal
        checkpoint["global_fatal"] = None
        semaphore = asyncio.Semaphore(self.settings.source_concurrency)
        fatal_event = asyncio.Event()
        checkpoint_lock = asyncio.Lock()
        sink_lock = asyncio.Lock()
        metrics: dict[str, Any] = {
            "requested": len(stock_ids),
            "expected_observations_per_stock": len(expected_strings),
            "observation_cadence": cadence,
            "skipped_checkpoint": len(initial_complete),
            "reused_complete": 0,
            "reused_valid_no_data": 0,
            "newly_fetched": 0,
            "partial_responses": 0,
            "physical_requests": 0,
            "retryable_pending": 0,
            "permanent_failed": 0,
            "checkpoint_state": checkpoint_state,
            "checkpoint_manifest_hash": manifest_hash,
            "requested_start_date": start_date,
            "requested_end_date": end_date,
            "universe_hash": universe_hash,
            "selection_policy": "sorted_stock_id_observation_resume",
            "success": 0,
            "usable_success": 0,
            "no_data": 0,
            "failed": 0,
            "rows": 0,
            "fatal_code": None,
            "previous_global_fatal": previous_global_fatal,
            "per_stock": {key: value for key, value in entries.items() if key in initial_complete},
        }

        async def persist() -> None:
            checkpoint["manifest"] = manifest
            checkpoint["manifest_hash"] = manifest_hash
            checkpoint["last_request"] = {"start_date": start_date, "end_date": end_date, "universe_hash": universe_hash, "pending_stock_count": len(pending), "expected_observation_count": len(expected_strings)}
            temporary = checkpoint_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(checkpoint_file)

        def coverage_fields(previous: dict[str, Any]) -> dict[str, Any]:
            covered = sorted(set(previous.get("covered_dates", [])) & requested_observations)
            unresolved = [day for day in expected_strings if day not in covered]
            return {
                "covered_dates": covered,
                "expected_observation_count": len(expected_strings),
                "verified_observation_count": len(covered),
                "unresolved_dates": unresolved,
                "observation_cadence": cadence,
            }

        async def mark_failure(stock_id: str, code: str, classification: str, *, global_fatal: bool = False) -> None:
            now = datetime.now(timezone.utc).isoformat()
            async with checkpoint_lock:
                previous = entries.get(stock_id, {})
                entry = {
                    **previous,
                    **coverage_fields(previous),
                    "rows": int(previous.get("rows", 0)),
                    "error_code": code,
                    "classification": classification,
                    "retry_count": int(previous.get("retry_count", 0)) + 1,
                    "last_attempt_at": now,
                    "next_eligible_retry_at": now if classification != "permanent_failed" else None,
                }
                entries[stock_id] = entry
                checkpoint["completed"] = sorted(set(checkpoint.get("completed", [])) - {stock_id})
                checkpoint["no_data_but_valid"] = sorted(set(checkpoint.get("no_data_but_valid", [])) - {stock_id})
                checkpoint["failed"] = [item for item in checkpoint.get("failed", []) if item.get("stock_id") != stock_id] + [{"stock_id": stock_id, **entry}]
                if classification == "permanent_failed":
                    checkpoint["permanent_failed"] = sorted(set(checkpoint.get("permanent_failed", [])) | {stock_id})
                if global_fatal:
                    checkpoint["global_fatal"] = code
                    metrics["fatal_code"] = code
                    fatal_event.set()
                metrics["failed"] += 1
                metrics["per_stock"][stock_id] = entry
                await persist()

        async def one(stock_id: str) -> None:
            if fatal_event.is_set():
                return
            async with semaphore:
                if fatal_event.is_set():
                    return
                request_start, request_end = request_ranges[stock_id]
                request_expected = {day for day in expected_strings if request_start <= day <= request_end}
                metrics["physical_requests"] += 1
                try:
                    records, meta = await asyncio.to_thread(self.fetch, dataset, stock_id, request_start, request_end)
                except FinMindError as exc:
                    global_fatal = exc.code in GLOBAL_PROVIDER_FAILURE_CODES
                    classification = "global_fatal" if global_fatal else ("permanent_failed" if exc.code == "NON_RETRYABLE_4XX" else "retryable_failed")
                    await mark_failure(stock_id, exc.code, classification, global_fatal=global_fatal)
                    if progress_callback:
                        progress_callback(f"{dataset} completed={metrics['newly_fetched'] + metrics['failed']}/{len(pending)}")
                    return
                if meta.get("pagination_complete") is False:
                    await mark_failure(stock_id, "INCOMPLETE_PROVIDER_COVERAGE", "retryable_failed")
                    if progress_callback:
                        progress_callback(f"{dataset} completed={metrics['newly_fetched'] + metrics['failed']}/{len(pending)}")
                    return

                verified_record_dates = _record_observation_dates(dataset, stock_id, records, request_expected)
                valid_no_data_dates, empty_reason = _validated_no_data_dates(dataset, meta, request_expected)
                try:
                    if record_sink and records:
                        async with sink_lock:
                            sink_result = record_sink(records)
                        if isinstance(sink_result, dict):
                            sink_dates = {str(value)[:10] for value in sink_result.get("accepted_dates", [])}
                            verified_record_dates &= sink_dates
                        elif int(sink_result) <= 0:
                            verified_record_dates.clear()
                except SchemaMismatch:
                    await mark_failure(stock_id, "STOCK_SCHEMA_MISMATCH", "permanent_failed")
                    if progress_callback:
                        progress_callback(f"{dataset} completed={metrics['newly_fetched'] + metrics['failed']}/{len(pending)}")
                    return

                previous = entries.get(stock_id, {})
                previous_covered = set(previous.get("covered_dates", [])) & requested_observations
                newly_verified = (verified_record_dates | valid_no_data_dates) - previous_covered
                if not newly_verified:
                    code = "EMPTY_RESPONSE_UNVERIFIED" if not records else "PARTIAL_RESPONSE_UNVERIFIED"
                    await mark_failure(stock_id, code, "retryable_failed")
                    if progress_callback:
                        progress_callback(f"{dataset} completed={metrics['newly_fetched'] + metrics['failed']}/{len(pending)}")
                    return

                covered = previous_covered | verified_record_dates | valid_no_data_dates
                unresolved = [day for day in expected_strings if day not in covered]
                all_record_dates = set(previous.get("verified_record_dates", [])) | verified_record_dates
                all_no_data_dates = set(previous.get("verified_no_data_dates", [])) | valid_no_data_dates
                complete = not unresolved
                classification = ("NEW_SUCCESS" if all_record_dates else "VALID_NO_DATA_FROM_PROVIDER") if complete else "PARTIAL_RETRYABLE"
                entry = {
                    "rows": int(previous.get("rows", 0)) + len(records),
                    "first_source_date": min(all_record_dates) if all_record_dates else previous.get("first_source_date"),
                    "last_source_date": max(all_record_dates) if all_record_dates else previous.get("last_source_date"),
                    "request_start": request_start,
                    "request_end": request_end,
                    "covered_dates": sorted(covered),
                    "verified_record_dates": sorted(all_record_dates),
                    "verified_no_data_dates": sorted(all_no_data_dates),
                    "expected_observation_count": len(expected_strings),
                    "verified_observation_count": len(covered),
                    "unresolved_dates": unresolved,
                    "observation_cadence": cadence,
                    "classification": classification,
                    "empty_reason": empty_reason,
                    "attempt": int(meta.get("attempt", 1)),
                    "pagination_complete": meta.get("pagination_complete"),
                    "retry_count": int(previous.get("retry_count", 0)) + (0 if complete else 1),
                }
                async with checkpoint_lock:
                    entries[stock_id] = entry
                    metrics["rows"] += len(records)
                    metrics["per_stock"][stock_id] = entry
                    checkpoint["failed"] = [item for item in checkpoint.get("failed", []) if item.get("stock_id") != stock_id]
                    checkpoint["completed"] = sorted(set(checkpoint.get("completed", [])) - {stock_id})
                    checkpoint["no_data_but_valid"] = sorted(set(checkpoint.get("no_data_but_valid", [])) - {stock_id})
                    if complete:
                        metrics["newly_fetched"] += 1
                        bucket = "completed" if all_record_dates else "no_data_but_valid"
                        checkpoint[bucket] = sorted(set(checkpoint.get(bucket, [])) | {stock_id})
                    else:
                        metrics["partial_responses"] += 1
                        metrics["failed"] += 1
                        checkpoint["failed"].append({"stock_id": stock_id, **entry, "error_code": "PARTIAL_OBSERVATION_COVERAGE"})
                    await persist()
                if progress_callback:
                    progress_callback(f"{dataset} completed={metrics['newly_fetched'] + metrics['failed']}/{len(pending)}")

        await asyncio.gather(*(one(stock_id) for stock_id in pending))

        def is_complete(stock_id: str) -> bool:
            covered = set(entries.get(stock_id, {}).get("covered_dates", []))
            return requested_observations <= covered

        complete_stocks = {stock_id for stock_id in stock_ids if is_complete(stock_id)}
        metrics["reused_complete"] = sum(1 for stock_id in initial_complete if entries.get(stock_id, {}).get("classification") != "VALID_NO_DATA_FROM_PROVIDER")
        metrics["reused_valid_no_data"] = sum(1 for stock_id in initial_complete if entries.get(stock_id, {}).get("classification") == "VALID_NO_DATA_FROM_PROVIDER")
        metrics["success"] = len(complete_stocks)
        metrics["usable_success"] = sum(1 for stock_id in complete_stocks if entries.get(stock_id, {}).get("verified_record_dates"))
        metrics["no_data"] = sum(1 for stock_id in complete_stocks if not entries.get(stock_id, {}).get("verified_record_dates"))
        metrics["permanent_failed"] = sum(1 for stock_id in stock_ids if not is_complete(stock_id) and entries.get(stock_id, {}).get("classification") == "permanent_failed")
        metrics["retryable_pending"] = sum(1 for stock_id in stock_ids if not is_complete(stock_id) and entries.get(stock_id, {}).get("classification") != "permanent_failed")
        metrics["verified_observations"] = sum(len(set(entries.get(stock_id, {}).get("covered_dates", [])) & requested_observations) for stock_id in stock_ids)
        metrics["unresolved_observations"] = len(stock_ids) * len(expected_strings) - metrics["verified_observations"]
        metrics["per_stock"] = {key: value for key, value in entries.items() if key in stock_ids}
        return metrics


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _policy_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def capability_evidence(client: FinMindClient, *, source_revision: str) -> dict[str, Any]:
    datasets = [
        "TaiwanStockInstitutionalInvestorsBuySellWide",
        "TaiwanStockShareholding",
        "TaiwanStockHoldingSharesPer",
        "TaiwanStockTradingDailyReport",
        "TaiwanStockTradingDailyReportSecIdAgg",
    ]
    results: list[dict[str, Any]] = []

    def append_probe(dataset: str, mode: str, production_used: bool) -> None:
        result = asdict(client.probe(dataset, mode=mode, production_used=production_used))
        result.update({
            "probe_only": True,
            "sanitized_request_mode": True,
            "secret_values_included": False,
        })
        results.append(result)

    for dataset in datasets:
        append_probe(dataset, "broad", False)
        if dataset != "TaiwanStockInfo":
            append_probe(dataset, "per_stock", dataset != "TaiwanStockTradingDailyReportSecIdAgg")
    dataset_policy = {
        "production_s_datasets": sorted(PRODUCTION_S_DATASETS),
        "reference_datasets": sorted(REFERENCE_DATASETS),
        "capability_only_datasets": sorted(CAPABILITY_ONLY_DATASETS),
        "forbidden_datasets": sorted(FORBIDDEN_DATASETS),
    }
    provider_policy = {
        "request_policy_version": REQUEST_POLICY_VERSION,
        "normalization_policy_version": NORMALIZATION_POLICY_VERSION,
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "incremental_checkpoint_version": INCREMENTAL_CHECKPOINT_VERSION,
        "empty_data_requires_exact_observation_dates": True,
        "pagination_false_fails_closed": True,
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "probe_time": datetime.now(timezone.utc).isoformat(),
        "source_revision": source_revision,
        "request_policy_version": REQUEST_POLICY_VERSION,
        "normalization_policy_version": NORMALIZATION_POLICY_VERSION,
        "dataset_policy_sha256": _policy_hash(dataset_policy),
        "provider_policy_sha256": _policy_hash(provider_policy),
        "dataset_policy": dataset_policy,
        "provider_policy": provider_policy,
        "policy": {
            "approved_datasets": sorted(PRODUCTION_S_DATASETS),
            "capability_only_datasets": sorted(CAPABILITY_ONLY_DATASETS),
            "raw_institutional_fallback": "disabled",
            "zero_rows_are_usable": False,
            "probe_path_can_ingest": False,
        },
        "results": results,
        "sanitized_request_mode": True,
        "secret_values_included": False,
    }
