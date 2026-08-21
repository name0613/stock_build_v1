from __future__ import annotations

from scripts.quota_budget_evidence import SOURCE_DATASETS, generate


def _runtime(*, complete: bool = True) -> dict:
    datasets = []
    for dataset in SOURCE_DATASETS:
        datasets.append({
            "dataset": dataset,
            "status": "SUCCESS" if complete else "PARTIAL",
            "rows_received_this_attempt": 100,
            "rows_accepted_this_attempt": 90,
            "rows_rejected_this_attempt": 10,
            "rows_versioned_this_attempt": 20,
            "observations_reused_this_attempt": 50,
            "metadata": {"coverage": {"requested": 1000, "success": 1000 if complete else 500, "physical_requests": 500, "verified_observations": 1000, "unresolved_observations": 0 if complete else 500, "selection_policy": "durable_round_robin_stock_cursor_observation_resume", "fair_cursor_start_stock_id": "1000", "fair_cursor_end_stock_id": "1500"}},
        })
    jobs = [
        {"dataset": SOURCE_DATASETS[0], "started_at": "2026-08-21T01:00:00+00:00", "checkpoint_state": {"physical_requests": 500, "success": 400, "fair_cursor_end_stock_id": "1400"}},
        {"dataset": SOURCE_DATASETS[0], "started_at": "2026-08-21T02:00:00+00:00", "checkpoint_state": {"physical_requests": 500, "success": 900, "fair_cursor_end_stock_id": "1900"}},
    ]
    return {"datasets": datasets, "jobs": jobs}


def test_quota_evidence_keeps_unverified_provider_limit_and_runtime_partial() -> None:
    evidence = generate({"stock_count": 1000}, _runtime(complete=False), None, None, 300)
    assert evidence["status"] == "PARTIAL_NOT_PROVEN"
    assert evidence["checks"]["provider_limit_directly_verified"] == "NOT_PROVEN"
    assert evidence["checks"]["observed_full_market_completion"] == "NOT_PROVEN"
    assert evidence["request_models"]["initial_bootstrap"]["minimum_quota_cycles"] == 5
    assert evidence["request_models"]["ordinary_trading_day"]["minimum_quota_cycles"] == 1
    assert evidence["secrets_included"] is False


def test_quota_evidence_pass_requires_direct_limit_renewal_progress_and_completion() -> None:
    evidence = generate({"stock_count": 1000}, _runtime(), 6000, 1250, 300)
    assert evidence["status"] == "PASS"
    assert all(value == "PASS" for value in evidence["checks"].values())
    assert evidence["renewal_history"]["qualifying_pairs"][0]["earlier_cursor"] == "1400"
    assert evidence["renewal_history"]["qualifying_pairs"][0]["later_cursor"] == "1900"
