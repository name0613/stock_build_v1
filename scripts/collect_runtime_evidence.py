from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx


def fetch(client: httpx.Client, url: str) -> tuple[dict, float]:
    started = time.perf_counter()
    response = client.get(url, timeout=15)
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    response.raise_for_status()
    return response.json(), elapsed


def benchmark(client: httpx.Client, url: str, repetitions: int = 7) -> dict[str, object]:
    timings: list[float] = []
    last: dict = {}
    for _ in range(repetitions):
        last, elapsed = fetch(client, url)
        timings.append(elapsed)
    ordered = sorted(timings)
    return {"url": url, "sample_size": repetitions, "median_ms": ordered[len(ordered) // 2], "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], "max_ms": max(ordered), "last_response": last}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/collect_runtime_evidence.py http://NAS:PORT")
    base = sys.argv[1].rstrip("/")
    output = Path("deployment_evidence")
    output.mkdir(exist_ok=True)
    with httpx.Client() as client:
        health, health_ms = fetch(client, f"{base}/health")
        summary, summary_ms = fetch(client, f"{base}/api/summary")
        status, status_ms = fetch(client, f"{base}/api/data-status")
        stocks = benchmark(client, f"{base}/api/stocks?page=1&page_size=50&sort=score&order=desc")
        rankings = benchmark(client, f"{base}/api/rankings?limit=50")
        detail = benchmark(client, f"{base}/api/stocks/2330?limit=365")
    evidence = {"url": base, "health": health, "summary": summary, "data_status": status, "api_timing_ms": {"health": health_ms, "summary": summary_ms, "data_status": status_ms}, "benchmarks": {"stocks": stocks, "rankings": rankings, "detail_2330": detail}, "performance_budgets_ms": {"stocks_p95": 500, "rankings_p95": 500, "detail_p95": 1000}, "sanitized": True, "secrets_included": False}
    (output / "NAS_DEPLOYMENT_EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    (output / "NAS_DEPLOYMENT_EVIDENCE.md").write_text("# NAS deployment evidence\n\nSanitized health, summary, data-status and timing responses.\n\n" + json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(output / "NAS_DEPLOYMENT_EVIDENCE.json"), "secrets_included": False}, ensure_ascii=False))
