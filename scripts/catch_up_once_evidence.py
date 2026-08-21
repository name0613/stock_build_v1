"""Run one production catch-up attempt and emit only sanitized coverage evidence."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path

try:
    from backend.app.config import get_settings
    from backend.app.db import SessionLocal, init_db
    from backend.app.finmind import FinMindClient
    from backend.app.ingestion import catch_up
    from backend.app.calendar import completed_source_end_date, market_session_state
except ModuleNotFoundError:
    from app.config import get_settings
    from app.db import SessionLocal, init_db
    from app.finmind import FinMindClient
    from app.ingestion import catch_up
    from app.calendar import completed_source_end_date, market_session_state


def source_revision() -> str:
    configured = os.getenv("SOURCE_REVISION", "").strip()
    if configured:
        return configured
    metadata_path = Path("/app/build-metadata.json")
    if metadata_path.exists():
        value = json.loads(metadata_path.read_text(encoding="utf-8")).get("source_revision")
        if value:
            return str(value)
    return "unavailable"


def run(target_date: date | None = None) -> dict[str, object]:
    init_db()
    db = SessionLocal()
    started = datetime.now(timezone.utc)
    resolved_target = target_date or completed_source_end_date()
    try:
        result = asyncio.run(catch_up(db, FinMindClient(get_settings()), end_date=resolved_target))
        return {
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "source_revision": source_revision(),
            "target_date": resolved_target.isoformat(),
            "target_date_policy": "explicit_override" if target_date else "completed_source_end_date_v1",
            "market_session": market_session_state(),
            "result": result,
            "sanitized": True,
            "secrets_included": False,
        }
    finally:
        db.close()


if __name__ == "__main__":
    target = date.fromisoformat(os.environ["CATCH_UP_TARGET_DATE"]) if os.getenv("CATCH_UP_TARGET_DATE") else None
    evidence = run(target)
    if os.getenv("CATCH_UP_EVIDENCE_STDOUT") == "1":
        print(json.dumps(evidence, ensure_ascii=False, default=str))
    else:
        output = Path("deployment_evidence/CATCH_UP_ATTEMPT_EVIDENCE.json")
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"path": str(output), "status": evidence["result"].get("status"), "secrets_included": False}, ensure_ascii=False))
