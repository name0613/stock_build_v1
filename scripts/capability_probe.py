from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from backend.app.config import get_settings
from backend.app.finmind import FinMindClient, capability_evidence


def source_revision() -> str:
    override = os.getenv("SOURCE_REVISION")
    if override:
        return override.strip()
    metadata_path = Path("/app/build-metadata.json")
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("source_revision"):
            return str(metadata["source_revision"])
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1], text=True).strip()


if __name__ == "__main__":
    output = Path("deployment_evidence")
    output.mkdir(exist_ok=True)
    evidence = capability_evidence(FinMindClient(get_settings()), source_revision=source_revision())
    if os.getenv("FINMIND_CAPABILITY_STDOUT") == "1":
        print(json.dumps(evidence, ensure_ascii=False))
    else:
        (output / "FINMIND_CAPABILITY_EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"path": str(output / "FINMIND_CAPABILITY_EVIDENCE.json"), "generated_at": datetime.now(timezone.utc).isoformat(), "secret_included": False}, ensure_ascii=False))
