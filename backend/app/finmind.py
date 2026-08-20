from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from .config import Settings, get_settings
from .scoring import parse_holding_level

logger = logging.getLogger(__name__)

ALLOWED_S_DATASETS = {
    "TaiwanStockInstitutionalInvestorsBuySellWide",
    "TaiwanStockInstitutionalInvestorsBuySell",
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
    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


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


class RawEvidenceStore:
    def __init__(self, root: Path):
        self.root = root

    def write(self, dataset: str, records: list[dict[str, Any]], parameters: dict[str, Any], source_date: str | None) -> dict[str, Any]:
        fetched_at = datetime.now(timezone.utc)
        date_part = source_date or "unknown"
        target_dir = self.root / dataset / f"date={date_part}"
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = []
        for record in records:
            enriched = dict(record)
            enriched["_evidence_source"] = "FinMind"
            enriched["_evidence_dataset"] = dataset
            enriched["_evidence_source_date"] = source_date
            enriched["_evidence_fetched_at"] = fetched_at.isoformat()
            payload.append(enriched)
        path = target_dir / f"part-{fetched_at.strftime('%Y%m%dT%H%M%S%fZ')}.parquet"
        table = pa.Table.from_pylist(payload or [{"_evidence_dataset": dataset, "_evidence_source_date": source_date, "_evidence_fetched_at": fetched_at.isoformat()}])
        pq.write_table(table, path, compression="zstd")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        metadata_path = path.with_suffix(".metadata.json")
        metadata_path.write_text(json.dumps({"source": "FinMind", "dataset": dataset, "parameters": parameters, "source_date": source_date, "fetched_at": fetched_at.isoformat(), "sha256": digest}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": str(path), "sha256": digest, "fetched_at": fetched_at.isoformat(), "records": len(records)}


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

    def _params(self, dataset: str, data_id: str | None, start_date: str | None, end_date: str | None) -> dict[str, str]:
        params = {"dataset": dataset}
        if data_id:
            params["data_id"] = data_id
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if self.settings.finmind_api_token:
            params["token"] = self.settings.finmind_api_token
        return params

    def fetch(self, dataset: str, data_id: str | None = None, start_date: str | None = None, end_date: str | None = None, *, persist_raw: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if dataset in FORBIDDEN_DATASETS:
            raise FinMindError("FORBIDDEN_DATASET", f"dataset is excluded by S-only policy: {dataset}")
        if dataset not in ALLOWED_S_DATASETS | REFERENCE_DATASETS:
            raise FinMindError("DATASET_NOT_ALLOWLISTED", f"dataset is not allowlisted: {dataset}")
        params = self._params(dataset, data_id, start_date, end_date)
        safe_params = {key: value for key, value in params.items() if key != "token"}
        last_error: FinMindError | None = None
        for attempt in range(self.settings.broker_max_retries + 1):
            try:
                with httpx.Client(base_url=self.settings.finmind_base_url, timeout=self.timeout, follow_redirects=True) as client:
                    response = client.get("/data", params=params)
                if response.status_code == 401:
                    raise FinMindError("AUTHENTICATION_FAILED", "FinMind authentication failed", 401)
                if response.status_code == 403:
                    raise FinMindError("ACCESS_DENIED", "FinMind plan permission denied", 403)
                if response.status_code == 429:
                    retry_after = response.headers.get("retry-after")
                    delay = float(retry_after) if retry_after and retry_after.replace('.', '', 1).isdigit() else 2 ** attempt
                    raise FinMindError("RATE_LIMITED", f"FinMind rate limit; retry after {delay:.1f}s", 429)
                if response.status_code >= 500:
                    raise FinMindError("UPSTREAM_5XX", "FinMind upstream server error", response.status_code)
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SchemaMismatch("SCHEMA_MISMATCH", "FinMind response was not valid JSON", response.status_code) from exc
                records = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(records, list):
                    raise SchemaMismatch("SCHEMA_MISMATCH", "FinMind response data field is not a list", response.status_code)
                normalized = [self._normalize_record(dataset, record) for record in records]
                source_date = self._latest_date(normalized)
                evidence = self.store.write(dataset, normalized, safe_params, source_date) if persist_raw else {"records": len(normalized)}
                return normalized, {"dataset": dataset, "parameters": safe_params, "source_date": source_date, "evidence": evidence, "attempt": attempt + 1}
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = FinMindError("TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "NETWORK_ERROR", "FinMind network request failed")
            except FinMindError as exc:
                last_error = exc
                if exc.code in {"AUTHENTICATION_FAILED", "ACCESS_DENIED", "SCHEMA_MISMATCH"}:
                    raise
            if attempt < self.settings.broker_max_retries:
                time.sleep(min(30, 2 ** attempt + random.random()))
        assert last_error is not None
        raise last_error

    def probe(self, dataset: str) -> CapabilityResult:
        try:
            records, meta = self.fetch(dataset, end_date=date.today().isoformat())
            return CapabilityResult(dataset, True, "GET /api/v4/data", meta.get("source_date"), len(records[:10]), None)
        except FinMindError as exc:
            return CapabilityResult(dataset, False, "GET /api/v4/data", None, 0, exc.code)

    def _normalize_record(self, dataset: str, record: Any) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise SchemaMismatch("SCHEMA_MISMATCH", f"{dataset} returned a non-object row")
        result = {str(k): v for k, v in record.items()}
        if dataset == "TaiwanStockHoldingSharesPer":
            level = result.get("HoldingSharesLevel") or result.get("holding_shares_level")
            result["holding_shares_threshold"] = parse_holding_level(level)
            if level is not None and result["holding_shares_threshold"] is None:
                raise SchemaMismatch("SCHEMA_MISMATCH", f"unrecognized HoldingSharesLevel: {level}")
            result["shares"] = result.get("shares") or result.get("HoldingShares")
        return result

    @staticmethod
    def _latest_date(records: list[dict[str, Any]]) -> str | None:
        dates = [str(row.get("date") or row.get("Date") or row.get("source_date")) for row in records if row.get("date") or row.get("Date") or row.get("source_date")]
        return max(dates) if dates else None

    async def fetch_broker_stocks(self, stock_ids: list[str], start_date: str, end_date: str, dataset: str = "TaiwanStockTradingDailyReport") -> dict[str, Any]:
        """Bounded async Sponsor-compatible path with checkpoint/resume semantics."""
        checkpoint_dir = self.settings.raw_root / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_file = checkpoint_dir / f"{dataset}-{start_date}-{end_date}.json"
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8")) if checkpoint_file.exists() else {"completed": [], "failed": []}
        completed = set(checkpoint.get("completed", []))
        limiter = RateLimiter(self.settings.broker_rate_per_second)
        semaphore = asyncio.Semaphore(self.settings.broker_concurrency)
        metrics = {"requested": len(stock_ids), "skipped_checkpoint": len(completed), "success": 0, "failed": 0, "rows": 0}

        async def one(stock_id: str) -> None:
            if stock_id in completed:
                return
            async with semaphore:
                await limiter.wait()
                try:
                    records, _ = await asyncio.to_thread(self.fetch, dataset, stock_id, start_date, end_date)
                    checkpoint["completed"].append(stock_id)
                    metrics["success"] += 1
                    metrics["rows"] += len(records)
                except FinMindError as exc:
                    checkpoint.setdefault("failed", []).append({"stock_id": stock_id, "code": exc.code})
                    metrics["failed"] += 1
                checkpoint_file.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")

        await asyncio.gather(*(one(stock_id) for stock_id in stock_ids))
        return metrics


def capability_evidence(client: FinMindClient) -> dict[str, Any]:
    datasets = [
        "TaiwanStockInstitutionalInvestorsBuySellWide",
        "TaiwanStockShareholding",
        "TaiwanStockHoldingSharesPer",
        "TaiwanStockTradingDailyReport",
        "TaiwanStockTradingDailyReportSecIdAgg",
    ]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "results": [asdict(client.probe(dataset)) for dataset in datasets]}

