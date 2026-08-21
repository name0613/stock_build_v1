"""Audit production broker-source isolation without exposing row payloads."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text

try:
    from backend.app.db import engine
except ModuleNotFoundError:  # running from the backend image
    from app.db import engine


OFFICIAL_SOURCE = "TaiwanStockTradingDailyReport"
CAPABILITY_SOURCE = "TaiwanStockTradingDailyReportSecIdAgg"


def collect() -> dict[str, object]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.connect() as connection:
        broker_sources = [dict(row._mapping) for row in connection.execute(text("SELECT source_dataset, count(*) AS rows FROM broker_daily GROUP BY source_dataset ORDER BY source_dataset"))]
        prohibited_rows = int(connection.execute(text("SELECT count(*) FROM broker_daily WHERE source_dataset IS DISTINCT FROM :official"), {"official": OFFICIAL_SOURCE}).scalar_one())
        prohibited_revisions = int(connection.execute(text("SELECT count(*) FROM source_revisions WHERE dataset = :capability"), {"capability": CAPABILITY_SOURCE}).scalar_one())
        constraint = connection.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_broker_daily_official_source' AND conrelid = 'broker_daily'::regclass")).scalar_one_or_none() if engine.dialect.name == "postgresql" else "model_check_constraint"
        audit_rows = [dict(row._mapping) for row in connection.execute(text("SELECT * FROM broker_source_isolation_audit ORDER BY applied_at"))] if "broker_source_isolation_audit" in tables else []
        quarantined_rows = int(connection.execute(text("SELECT count(*) FROM broker_daily_source_quarantine")).scalar_one()) if "broker_daily_source_quarantine" in tables else 0
        quarantined_revisions = int(connection.execute(text("SELECT count(*) FROM broker_source_revision_quarantine")).scalar_one()) if "broker_source_revision_quarantine" in tables else 0
        affected_stocks = int(connection.execute(text("SELECT count(*) FROM broker_source_affected_stocks")).scalar_one()) if "broker_source_affected_stocks" in tables else 0
        pending_rebuilds = int(connection.execute(text("SELECT count(*) FROM broker_source_affected_stocks WHERE remediation_state <> 'REBUILT_FROM_OFFICIAL_SOURCE'")).scalar_one()) if "broker_source_affected_stocks" in tables else 0
        remediation_states = [dict(row._mapping) for row in connection.execute(text("SELECT remediation_state, count(*) AS stocks FROM broker_source_affected_stocks GROUP BY remediation_state ORDER BY remediation_state"))] if "broker_source_affected_stocks" in tables else []
        score_counts = [dict(row._mapping) for row in connection.execute(text("SELECT score_version, count(*) AS rows FROM accumulation_scores GROUP BY score_version ORDER BY score_version"))]
    build_metadata = {}
    metadata_path = Path("/app/build-metadata.json")
    if metadata_path.exists():
        build_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    status = "PASS" if prohibited_rows == 0 and prohibited_revisions == 0 and pending_rebuilds == 0 and constraint else "FAIL"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "source_revision": build_metadata.get("source_revision", os.getenv("SOURCE_REVISION", "unknown")),
        "official_broker_source": OFFICIAL_SOURCE,
        "capability_only_source": CAPABILITY_SOURCE,
        "broker_sources": broker_sources,
        "prohibited_broker_rows": prohibited_rows,
        "prohibited_source_revisions": prohibited_revisions,
        "database_constraint": constraint,
        "quarantined_broker_rows": quarantined_rows,
        "quarantined_source_revisions": quarantined_revisions,
        "affected_stocks": affected_stocks,
        "pending_authoritative_rebuilds": pending_rebuilds,
        "remediation_states": remediation_states,
        "migration_audit": audit_rows,
        "score_counts": score_counts,
        "payloads_included": False,
        "secrets_included": False,
        "sanitized": True,
    }


if __name__ == "__main__":
    result = collect()
    if os.getenv("BROKER_SOURCE_AUDIT_STDOUT") == "1":
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        output = Path("deployment_evidence/BROKER_SOURCE_ISOLATION_EVIDENCE.json")
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"path": str(output), "status": result["status"], "secrets_included": False}, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
