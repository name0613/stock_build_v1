"""Capture source-bound, sanitized FinMind quota evidence."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from backend.app.config import get_settings
    from backend.app.finmind import FinMindClient
except ModuleNotFoundError:  # running from the backend/worker image
    from app.config import get_settings
    from app.finmind import FinMindClient


def source_revision() -> str:
    override = os.getenv("SOURCE_REVISION")
    if override:
        return override.strip()
    metadata_path = Path("/app/build-metadata.json")
    if metadata_path.exists():
        value = json.loads(metadata_path.read_text(encoding="utf-8")).get("source_revision")
        if value:
            return str(value)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


if __name__ == "__main__":
    output = Path("deployment_evidence/FINMIND_PROVIDER_QUOTA_EVIDENCE.json")
    output.parent.mkdir(exist_ok=True)
    evidence = FinMindClient(get_settings()).provider_quota(source_revision=source_revision())
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(output), "status": evidence["status"], "source_revision": evidence["source_revision"], "secrets_included": False}, ensure_ascii=False))
