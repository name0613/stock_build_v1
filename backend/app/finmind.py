from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
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
from .scoring import parse_holding_level

logger = logging.getLogger(__name__)
# httpx's INFO request logger includes the complete URL.  FinMind carries the
# token as a query parameter for compatibility, so request URLs must never be
# emitted by application or worker logs.
for _http_logger_name in ("httpx", "httpcore"):
    logging.getLogger(_http_logger_name).setLevel(logging.WARNING)

ALLOWED_S_DATASETS = {
    "TaiwanStockInstitutionalInvestorsBuySellWide",
    "TaiwanStockShareholding",
    "TaiwanStockHoldingSharesPer",
    "TaiwanStockTradingDailyReport",
    "TaiwanStockTradingDailyReportSecIdAgg",
}
REFERENCE_DATASETS = {"TaiwanStockInfo", "TaiwanStockPrice", "TaiwanSecuritiesTraderInfo"}
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
        if dataset in FORBIDDEN_DATASETS:
            raise FinMindError("FORBIDDEN_DATASET", f"dataset is excluded by S-only policy: {dataset}")
        if dataset not in ALLOWED_S_DATASETS | REFERENCE_DATASETS:
            raise FinMindError("DATASET_NOT_ALLOWLISTED", f"dataset is not allowlisted: {dataset}")
        endpoint, params = self._request_spec(dataset, data_id, start_date, end_date, securities_trader_id)
        safe_params = {key: value for key, value in params.items() if key != "token"}
        last_error: FinMindError | None = None
        for attempt in range(self.settings.broker_max_retries + 1):
            try:
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
                records = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(records, list):
                    raise SchemaMismatch("SCHEMA_MISMATCH", "FinMind response data field is not a list", response.status_code)
                if isinstance(payload.get("msg"), str) and any(term in payload["msg"].lower() for term in ("quota", "rate limit", "limit exceeded")):
                    raise FinMindError("QUOTA_EXHAUSTED", "FinMind quota exhausted; request deferred", response.status_code)
                normalized = [self._normalize_record(dataset, record) for record in records]
                source_date = self._latest_date(normalized)
                evidence = self.store.write(dataset, normalized, safe_params, source_date) if persist_raw else {"records": len(normalized)}
                return normalized, {"dataset": dataset, "parameters": safe_params, "source_date": source_date, "evidence": evidence, "attempt": attempt + 1}
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

    def probe(self, dataset: str) -> CapabilityResult:
        try:
            end_date = date.today().isoformat()
            start_date = (date.today() - timedelta(days=30)).isoformat()
            data_id = None if dataset == "TaiwanStockInfo" else "2330"
            trader_id = "075T" if dataset == "TaiwanStockTradingDailyReportSecIdAgg" else None
            records, meta = self.fetch(dataset, data_id=data_id, start_date=None if dataset == "TaiwanStockInfo" else start_date, end_date=None if dataset == "TaiwanStockInfo" else end_date, securities_trader_id=trader_id)
            method = "GET /api/v4/data" if dataset not in {"TaiwanStockTradingDailyReport", "TaiwanStockTradingDailyReportSecIdAgg"} else f"GET /api/v4/{'taiwan_stock_trading_daily_report' if dataset.endswith('Report') else 'taiwan_stock_trading_daily_report_secid_agg'}"
            fields = sorted({key for row in records[:3] for key in row})
            return CapabilityResult(dataset, True, method, meta.get("source_date"), len(records[:10]), None, 200, fields, {"data_id": data_id, "start_date": start_date, "end_date": end_date, "securities_trader_id": trader_id}, dataset != "TaiwanStockTradingDailyReportSecIdAgg")
        except FinMindError as exc:
            return CapabilityResult(dataset, False, "GET /api/v4/data", None, 0, exc.code, exc.status_code, [], {"data_id": data_id, "start_date": start_date, "end_date": end_date, "securities_trader_id": trader_id}, False)

    def _normalize_record(self, dataset: str, record: Any) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise SchemaMismatch("SCHEMA_MISMATCH", f"{dataset} returned a non-object row")
        result = {str(k): v for k, v in record.items()}
        if dataset == "TaiwanStockHoldingSharesPer":
            level = result.get("HoldingSharesLevel") or result.get("holding_shares_level")
            result["holding_shares_threshold"] = parse_holding_level(level)
            if level is not None and result["holding_shares_threshold"] is None and str(level).strip().lower() not in {"total", "all"}:
                result["_schema_warning"] = "UNRECOGNIZED_HOLDING_LEVEL"
            result["shares"] = result.get("shares") or result.get("HoldingShares") or result.get("unit")
        return result

    @staticmethod
    def _latest_date(records: list[dict[str, Any]]) -> str | None:
        dates = [str(row.get("date") or row.get("Date") or row.get("source_date")) for row in records if row.get("date") or row.get("Date") or row.get("source_date")]
        return max(dates) if dates else None

    async def fetch_broker_stocks(self, stock_ids: list[str], start_date: str, end_date: str, dataset: str = "TaiwanStockTradingDailyReport", *, record_sink: Callable[[list[dict[str, Any]]], int] | None = None) -> dict[str, Any]:
        """Bounded async Sponsor-compatible path with checkpoint/resume semantics."""
        checkpoint_dir = self.settings.raw_root / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_file = checkpoint_dir / f"{dataset}-{start_date}-{end_date}.json"
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8")) if checkpoint_file.exists() else {"completed": [], "failed": [], "permanent_failed": []}
        completed = set(checkpoint.get("completed", []))
        failed_keys = {item.get("key") for item in checkpoint.get("failed", [])}
        limiter = RateLimiter(self.settings.broker_rate_per_second)
        semaphore = asyncio.Semaphore(self.settings.broker_concurrency)
        checkpoint_lock = asyncio.Lock()
        metrics = {"requested": len(stock_ids), "skipped_checkpoint": len(completed), "success": 0, "failed": 0, "rows": 0, "retries": 0, "_records": []}

        days = [(start_date if start_date == end_date else start_date)]
        if dataset == "TaiwanStockTradingDailyReport":
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
            days = [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]

        async def persist() -> None:
            temporary = checkpoint_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(checkpoint_file)

        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue(maxsize=max(1, self.settings.broker_concurrency * 2))

        async def one(stock_id: str, requested_date: str) -> None:
            checkpoint_key = f"{stock_id}:{requested_date}"
            if checkpoint_key in completed or checkpoint_key in failed_keys:
                return
            async with semaphore:
                await limiter.wait()
                try:
                    records, _ = await asyncio.to_thread(self.fetch, dataset, stock_id, requested_date, requested_date)
                    async with checkpoint_lock:
                        if checkpoint_key not in completed:
                            checkpoint["completed"].append(checkpoint_key)
                            completed.add(checkpoint_key)
                        metrics["success"] += 1
                        metrics["rows"] += len(records)
                        metrics["_records"].extend(records)
                        if record_sink:
                            record_sink(records)
                        await persist()
                except FinMindError as exc:
                    async with checkpoint_lock:
                        if checkpoint_key not in failed_keys:
                            checkpoint.setdefault("failed", []).append({"key": checkpoint_key, "stock_id": stock_id, "requested_date": requested_date, "code": exc.code})
                            failed_keys.add(checkpoint_key)
                        if exc.code in {"AUTHENTICATION_FAILED", "ACCESS_DENIED", "QUOTA_EXHAUSTED", "SCHEMA_MISMATCH", "NON_RETRYABLE_4XX"}:
                            checkpoint.setdefault("permanent_failed", []).append(checkpoint_key)
                        metrics["failed"] += 1
                        await persist()

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
        for stock_id in stock_ids:
            for requested_date in days:
                await queue.put((stock_id, requested_date))
        await queue.join()
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)
        return metrics


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def capability_evidence(client: FinMindClient) -> dict[str, Any]:
    datasets = [
        "TaiwanStockInstitutionalInvestorsBuySellWide",
        "TaiwanStockShareholding",
        "TaiwanStockHoldingSharesPer",
        "TaiwanStockTradingDailyReport",
        "TaiwanStockTradingDailyReportSecIdAgg",
    ]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "results": [asdict(client.probe(dataset)) for dataset in datasets]}
