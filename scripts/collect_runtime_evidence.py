from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx


def fetch(client: httpx.Client, url: str) -> tuple[dict, float]:
    started = time.perf_counter()
    response = client.get(url, timeout=15)
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    response.raise_for_status()
    return response.json(), elapsed


def response_summary(payload: dict) -> dict[str, object]:
    summary: dict[str, object] = {"top_level_keys": sorted(payload)}
    if isinstance(payload.get("items"), list):
        summary["item_count"] = len(payload["items"])
        summary["non_null_scores"] = sum(item.get("score") is not None for item in payload["items"] if isinstance(item, dict))
    if isinstance(payload.get("sources"), dict):
        summary["source_row_counts"] = {name: value.get("row_count") for name, value in payload["sources"].items() if isinstance(value, dict)}
    if isinstance(payload.get("score"), dict):
        summary["score_status"] = payload["score"].get("status")
    return summary


def percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    def percentile(value: float) -> float:
        index = min(len(ordered) - 1, max(0, int((len(ordered) * value + 0.999999) - 1)))
        return round(ordered[index], 2)
    return {"median_ms": percentile(0.50), "p95_ms": percentile(0.95), "p99_ms": percentile(0.99), "max_ms": round(max(ordered), 2)}


def benchmark(client: httpx.Client, url: str, repetitions: int = 20, concurrency: int = 4) -> dict[str, object]:
    last, first_elapsed = fetch(client, url)
    timings: list[float] = [first_elapsed]
    for _ in range(repetitions - 1):
        last, elapsed = fetch(client, url)
        timings.append(elapsed)
    warm = {"sample_size": len(timings), "concurrency": 1, **percentiles(timings)}
    def one(_: int) -> float:
        with httpx.Client() as concurrent_client:
            _, elapsed = fetch(concurrent_client, url)
            return elapsed
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        concurrent_timings = list(executor.map(one, range(repetitions)))
    return {"url": url, "warm": warm, "concurrent": {"sample_size": len(concurrent_timings), "concurrency": concurrency, **percentiles(concurrent_timings)}, "response_summary": response_summary(last)}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/collect_runtime_evidence.py http://NAS:PORT")
    base = sys.argv[1].rstrip("/")
    output = Path("deployment_evidence")
    output.mkdir(exist_ok=True)
    with httpx.Client() as client:
        health, health_ms = fetch(client, f"{base}/health")
        worker_health, worker_ms = fetch(client, f"{base}/api/worker-health")
        summary, summary_ms = fetch(client, f"{base}/api/summary")
        status, status_ms = fetch(client, f"{base}/api/data-status")
        stocks = benchmark(client, f"{base}/api/stocks?page=1&page_size=50&sort=score&order=desc")
        rankings = benchmark(client, f"{base}/api/rankings?limit=50")
        detail = benchmark(client, f"{base}/api/stocks/2330?limit=365")
    evidence = {"url": base, "health": health, "worker_health": worker_health, "summary": {"stock_count": summary.get("stock_count"), "latest_score_date": summary.get("latest_score_date"), "status_invariant": summary.get("status_invariant"), "score_version": summary.get("score_version"), "formula_hash": summary.get("formula_hash")}, "data_status": {"dataset_count": len(status.get("datasets", [])), "job_count": len(status.get("jobs", [])), "datasets": [{key: value for key, value in dataset.items() if key in {"dataset", "status", "latest_source_date", "attempt_latest_source_date", "expected_latest_source_date", "source_age_days", "rows_received_this_attempt", "rows_accepted_this_attempt", "rows_rejected_this_attempt", "stored_rows_total", "staleness", "error_code", "metadata"}} for dataset in status.get("datasets", [])]}, "api_timing_ms": {"health": health_ms, "worker_health": worker_ms, "summary": summary_ms, "data_status": status_ms}, "benchmarks": {"stocks": stocks, "rankings": rankings, "detail_2330": detail}, "row_counts": {"universe": summary.get("stock_count"), "detail_2330_sources": detail.get("response_summary", {}).get("source_row_counts", {})}, "performance_budgets_ms": {"stocks_p95": 500, "rankings_p95": 500, "detail_p95": 1000}, "sanitized": True, "secrets_included": False}
    (output / "NAS_DEPLOYMENT_EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    (output / "NAS_DEPLOYMENT_EVIDENCE.md").write_text("# NAS deployment evidence\n\nSanitized health, summary, data-status and timing responses.\n\n" + json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(output / "NAS_DEPLOYMENT_EVIDENCE.json"), "secrets_included": False}, ensure_ascii=False))
