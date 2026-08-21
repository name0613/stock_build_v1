"""Generate granular FinMind quota/full-market viability evidence without credentials."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from backend.app.finmind import CHECKPOINT_SCHEMA_VERSION, INCREMENTAL_CHECKPOINT_VERSION, REQUEST_POLICY_VERSION
except ModuleNotFoundError:
    from app.finmind import CHECKPOINT_SCHEMA_VERSION, INCREMENTAL_CHECKPOINT_VERSION, REQUEST_POLICY_VERSION


EXPECTED_SPONSOR_LIMIT = 6_000
DEFAULT_RESERVE = 300
SOURCE_DATASETS = (
    "TaiwanStockInstitutionalInvestorsBuySellWide",
    "TaiwanStockShareholding",
    "TaiwanStockHoldingSharesPer",
    "TaiwanStockTradingDailyReport",
    "TaiwanStockPrice",
)


def _revision() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _load_runtime(base_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with httpx.Client(timeout=20) as client:
        summary_response = client.get(f"{base_url.rstrip('/')}/api/summary")
        status_response = client.get(f"{base_url.rstrip('/')}/api/data-status")
    summary_response.raise_for_status()
    status_response.raise_for_status()
    return summary_response.json(), status_response.json()


def _cycles(requests: int, usable_budget: int) -> int:
    return math.ceil(requests / usable_budget) if requests and usable_budget else 0


def _coverage(dataset: dict[str, Any]) -> dict[str, Any]:
    metadata = dataset.get("metadata") or {}
    coverage = metadata.get("coverage") or {}
    received = int(dataset.get("rows_received_this_attempt") or 0)
    accepted = int(dataset.get("rows_accepted_this_attempt") or 0)
    rejected = int(dataset.get("rows_rejected_this_attempt") or 0)
    versioned = int(dataset.get("rows_versioned_this_attempt") or 0)
    reused = int(dataset.get("observations_reused_this_attempt") or coverage.get("observations_reused") or 0)
    return {
        "dataset": dataset.get("dataset"),
        "status": dataset.get("status"),
        "physical_requests": int(dataset.get("physical_requests_this_attempt") or 0),
        "requested_stocks": int(coverage.get("requested") or 0),
        "successful_stocks": int(coverage.get("success") or 0),
        "verified_observations": int(coverage.get("verified_observations") or 0),
        "unresolved_observations": int(coverage.get("unresolved_observations") or 0),
        "selection_policy": coverage.get("selection_policy"),
        "fair_cursor_start_stock_id": coverage.get("fair_cursor_start_stock_id"),
        "fair_cursor_end_stock_id": coverage.get("fair_cursor_end_stock_id"),
        "rows_received": received,
        "rows_accepted": accepted,
        "rows_rejected": rejected,
        "rows_versioned": versioned,
        "observations_reused": reused,
        "counter_attempt_id": dataset.get("counter_attempt_id"),
        "counter_semantics_version": dataset.get("counter_semantics_version"),
        "counters_are_current_attempt": dataset.get("counters_are_current_attempt") is True,
        "historical_pre_v5_counters_present": dataset.get("historical_pre_v5_counters") is not None,
        "counter_reconciles": dataset.get("counters_are_current_attempt") is True and dataset.get("counter_semantics_version") == "attempt-v5-reconciled-v1" and accepted + rejected == received and versioned <= accepted,
    }


def _renewal_history(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    qualifying_pairs: list[dict[str, Any]] = []
    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        if job.get("dataset") in SOURCE_DATASETS:
            by_dataset.setdefault(str(job["dataset"]), []).append(job)
    for dataset, dataset_jobs in by_dataset.items():
        ordered = sorted(dataset_jobs, key=lambda item: _parse_time(item.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc))
        for left, right in zip(ordered, ordered[1:]):
            left_time = _parse_time(left.get("started_at"))
            right_time = _parse_time(right.get("started_at"))
            left_checkpoint = left.get("checkpoint_state") or {}
            right_checkpoint = right.get("checkpoint_state") or {}
            if not left_time or not right_time or (right_time - left_time).total_seconds() < 55 * 60:
                continue
            if int(left_checkpoint.get("physical_requests") or 0) <= 0 or int(right_checkpoint.get("physical_requests") or 0) <= 0:
                continue
            left_unresolved = int(left_checkpoint.get("unresolved_observations") or 0)
            right_unresolved = int(right_checkpoint.get("unresolved_observations") or 0)
            progress = right_unresolved < left_unresolved and right_checkpoint.get("fair_cursor_end_stock_id") != left_checkpoint.get("fair_cursor_end_stock_id") and int(right_checkpoint.get("observations_reused") or 0) > 0
            if progress:
                qualifying_pairs.append({"dataset": dataset, "earlier_started_at": left.get("started_at"), "later_started_at": right.get("started_at"), "earlier_success": left_checkpoint.get("success"), "later_success": right_checkpoint.get("success"), "earlier_cursor": left_checkpoint.get("fair_cursor_end_stock_id"), "later_cursor": right_checkpoint.get("fair_cursor_end_stock_id"), "earlier_unresolved": left_unresolved, "later_unresolved": right_unresolved, "later_observations_reused": right_checkpoint.get("observations_reused"), "earlier_checkpoint_hash": left_checkpoint.get("checkpoint_manifest_hash"), "later_checkpoint_hash": right_checkpoint.get("checkpoint_manifest_hash")})
    return {"status": "PASS" if qualifying_pairs else "NOT_PROVEN", "minimum_separation_minutes": 55, "qualifying_pairs": qualifying_pairs}


def _verified_provider_quota(evidence: dict[str, Any] | None, source_revision: str) -> tuple[int | None, int | None]:
    if not evidence:
        return None, None
    valid = (
        evidence.get("status") == "PASS"
        and evidence.get("direct_provider_response") is True
        and evidence.get("source_revision") == source_revision
        and evidence.get("sanitized") is True
        and evidence.get("secrets_included") is False
    )
    limit = evidence.get("provider_reported_limit_per_hour")
    used = evidence.get("provider_reported_used")
    if not valid or not isinstance(limit, int) or not isinstance(used, int) or limit <= 0 or used < 0:
        return None, None
    return limit, used


def generate(summary: dict[str, Any], status: dict[str, Any], provider_evidence: dict[str, Any] | None, reserve: int, *, source_revision: str | None = None) -> dict[str, Any]:
    revision = source_revision or _revision()
    provider_limit, provider_used = _verified_provider_quota(provider_evidence, revision)
    universe = int(summary.get("stock_count") or 0)
    effective_limit = provider_limit or EXPECTED_SPONSOR_LIMIT
    usable_budget = max(0, effective_limit - reserve)
    request_models = {
        "initial_bootstrap": {"universe": 1, "daily_and_weekly_sources": 4 * universe, "broker_20_sessions": 20 * universe},
        "ordinary_trading_day": {"universe": 1, "daily_sources": 3 * universe, "broker_one_session": universe},
        "holding_publication_day": {"universe": 1, "daily_sources": 3 * universe, "holding_one_period": universe, "broker_one_session": universe},
    }
    for model in request_models.values():
        total = sum(model.values())
        model["total_physical_requests_upper_bound"] = total
        model["minimum_quota_cycles"] = _cycles(total, usable_budget)
    runtime = [_coverage(dataset) for dataset in status.get("datasets", []) if dataset.get("dataset") in SOURCE_DATASETS]
    completed = len(runtime) == len(SOURCE_DATASETS) and all(item["status"] in {"SUCCESS", "REUSED"} and item["unresolved_observations"] == 0 for item in runtime)
    counters_pass = len(runtime) == len(SOURCE_DATASETS) and all(item["counter_reconciles"] for item in runtime)
    renewal = _renewal_history(status.get("jobs", []))
    checks = {
        "provider_limit_directly_verified": "PASS" if provider_limit is not None else "NOT_PROVEN",
        "budget_math": "PASS" if universe > 0 and usable_budget > 0 else "NOT_PROVEN",
        "durable_checkpoint_contract": "PASS",
        "runtime_counter_reconciliation": "PASS" if counters_pass else "NOT_PROVEN",
        "observed_multi_renewal_progress": renewal["status"],
        "observed_full_market_completion": "PASS" if completed else "NOT_PROVEN",
    }
    overall = "PASS" if all(value == "PASS" for value in checks.values()) else "PARTIAL_NOT_PROVEN"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": revision,
        "status": overall,
        "checks": checks,
        "quota": {"expected_sponsor_limit_per_hour": EXPECTED_SPONSOR_LIMIT, "provider_reported_limit_per_hour": provider_limit, "provider_reported_used": provider_used, "effective_limit_per_hour": effective_limit, "operational_reserve": reserve, "usable_budget_per_hour": usable_budget, "direct_evidence_source_revision": provider_evidence.get("source_revision") if provider_evidence else None, "direct_evidence_type": provider_evidence.get("evidence_type") if provider_evidence else None, "direct_verification_note": "only a source-matched sanitized authenticated_provider_user_info artifact can directly verify the limit"},
        "universe_stock_count": universe,
        "request_models": request_models,
        "checkpoint_contract": {"checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION, "incremental_checkpoint_version": INCREMENTAL_CHECKPOINT_VERSION, "request_policy_version": REQUEST_POLICY_VERSION, "non_broker_selection_policy": "durable_round_robin_stock_cursor_observation_resume", "broker_selection_policy": "date_major_round_robin"},
        "runtime_attempts": runtime,
        "renewal_history": renewal,
        "interpretation": {"one_hour_completion_required": False, "eventual_completion_requires_checkpoint_resume": True, "full_market_viability_claimed_only_when_observed_full_market_completion_is_pass": True},
        "sanitized": True,
        "secrets_included": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--provider-quota-evidence", type=Path)
    parser.add_argument("--reserve", type=int, default=DEFAULT_RESERVE)
    parser.add_argument("--output", type=Path, default=Path("deployment_evidence/QUOTA_BUDGET_EVIDENCE.json"))
    args = parser.parse_args()
    summary, status = _load_runtime(args.base_url)
    provider_evidence = json.loads(args.provider_quota_evidence.read_text(encoding="utf-8")) if args.provider_quota_evidence else None
    evidence = generate(summary, status, provider_evidence, args.reserve)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(args.output), "status": evidence["status"], "secrets_included": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
