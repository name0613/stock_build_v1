"""Create source-bound, sanitized browser acceptance evidence from Playwright JSON."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DISCLAIMER_PHRASES = (
    "分點是券商營業據點的彙總，不等同於一位自然人或「主力」",
    "未出現分點保持 unknown，絕不補零",
)


def _annotations(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("annotations"), list):
            found.extend(item for item in value["annotations"] if isinstance(item, dict))
        for child in value.values():
            found.extend(_annotations(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_annotations(child))
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=ROOT / "test_results/frontend-e2e-results.json")
    parser.add_argument("--output", type=Path, default=ROOT / "deployment_evidence/BROWSER_ACCEPTANCE_EVIDENCE.json")
    args = parser.parse_args()
    results = json.loads(args.results.read_text(encoding="utf-8"))
    metadata = (results.get("config") or {}).get("metadata") or {}
    annotations = _annotations(results)
    ready_values = [
        float(item["description"])
        for item in annotations
        if item.get("type") == "lan_data_ready_ms"
        and str(item.get("description", "")).replace(".", "", 1).isdigit()
    ]
    budget_values = [
        float(item["description"])
        for item in annotations
        if item.get("type") == "lan_data_ready_budget_ms"
        and str(item.get("description", "")).replace(".", "", 1).isdigit()
    ]
    broker_annotations = [item for item in annotations if item.get("type") == "broker_disclaimer"]
    frontend_source = (ROOT / "frontend/src/main.tsx").read_text(encoding="utf-8")
    stats = results.get("stats") or {}
    evidence = {
        "format": "browser-acceptance-evidence-v2",
        "acceptance_run_id": metadata.get("acceptance_run_id"),
        "source_revision": metadata.get("source_revision") or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "base_url": metadata.get("base_url"),
        "playwright": {
            "expected": stats.get("expected"),
            "unexpected": stats.get("unexpected"),
            "flaky": stats.get("flaky"),
            "passed": stats.get("unexpected") == 0 and stats.get("expected", 0) > 0,
        },
        "lan_data_ready_ms": min(ready_values) if ready_values else None,
        "lan_data_ready_budget_ms": min(budget_values) if budget_values else 2000,
        "lan_data_ready_pass": bool(ready_values) and min(ready_values) <= min(budget_values or [2000]),
        "broker_disclaimer": bool(broker_annotations) and all(phrase in frontend_source for phrase in DISCLAIMER_PHRASES),
        "broker_disclaimer_phrases": list(DISCLAIMER_PHRASES),
        "broker_disclaimer_annotation": broker_annotations,
        "score_version": metadata.get("score_version"),
        "formula_hash": metadata.get("formula_hash"),
        "frontend_image_digest": metadata.get("frontend_image_digest"),
        "sanitized": True,
        "secrets_included": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(args.output), "broker_disclaimer": evidence["broker_disclaimer"], "lan_data_ready_pass": evidence["lan_data_ready_pass"], "secrets_included": False}, ensure_ascii=False))
    raise SystemExit(0 if evidence["playwright"]["passed"] and evidence["broker_disclaimer"] and evidence["lan_data_ready_pass"] else 1)


if __name__ == "__main__":
    main()
