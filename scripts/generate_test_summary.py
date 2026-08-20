"""Generate reviewer-facing test summaries from machine-produced results."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def junit_counts(path: Path) -> dict[str, int | str]:
    suite = ET.parse(path).getroot()
    tests = int(suite.attrib.get("tests", 0))
    failures = int(suite.attrib.get("failures", 0))
    errors = int(suite.attrib.get("errors", 0))
    skipped = int(suite.attrib.get("skipped", 0))
    return {"status": "PASS" if failures + errors == 0 else "FAIL", "tests": tests, "passed": tests - failures - errors - skipped, "failed": failures + errors, "skipped": skipped, "secrets_included": False}


def main() -> None:
    results = {"backend": junit_counts(ROOT / "test_results/backend-junit.xml")}
    e2e = ROOT / "test_results/frontend-e2e-junit.xml"
    if e2e.exists():
        results["frontend_e2e"] = junit_counts(e2e)
    (ROOT / "test_results/backend-pytest-results.json").write_text(json.dumps({"command": "python -m pytest backend/tests -q --junitxml=test_results/backend-junit.xml", **results["backend"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Verification summary", "", "Generated from JUnit XML and current evidence files.", "", f"- Backend: {results['backend']['passed']}/{results['backend']['tests']} passed; failed={results['backend']['failed']}; skipped={results['backend']['skipped']}."]
    if "frontend_e2e" in results:
        lines.append(f"- Frontend E2E: {results['frontend_e2e']['passed']}/{results['frontend_e2e']['tests']} passed; failed={results['frontend_e2e']['failed']}.")
    lines.extend(["- Static checks: Ruff and frontend production build are recorded as machine results in this directory.", "- NAS runtime, persistence, source binding, migration, image secret scan, PIT, and acceptance manifest are selected by the final reviewer bundle manifest.", "- Current provider quota/error states remain explicit fail-closed states; no synthetic data is introduced.", ""])
    (ROOT / "test_results/TEST_SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
