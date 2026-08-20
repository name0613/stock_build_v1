from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from backend.app.config import get_settings
from backend.app.finmind import FinMindClient, capability_evidence


if __name__ == "__main__":
    output = Path("deployment_evidence")
    output.mkdir(exist_ok=True)
    evidence = capability_evidence(FinMindClient(get_settings()))
    (output / "FINMIND_CAPABILITY_EVIDENCE.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(output / "FINMIND_CAPABILITY_EVIDENCE.json"), "generated_at": datetime.now(timezone.utc).isoformat(), "secret_included": False}, ensure_ascii=False))

