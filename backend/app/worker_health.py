from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any


def _read_heartbeat(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "missing", "ready": False}


def evaluate_health(payload: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Evaluate process and scheduler progress separately for Docker/API health."""
    now = now or datetime.now(timezone.utc)
    try:
        heartbeat_at = datetime.fromisoformat(str(payload["last_heartbeat_at"]))
        scheduler_at = datetime.fromisoformat(str(payload["last_scheduler_heartbeat_at"]))
        heartbeat_age = max(0, int((now - heartbeat_at).total_seconds()))
        scheduler_age = max(0, int((now - scheduler_at).total_seconds()))
    except (KeyError, TypeError, ValueError):
        return {"status": "degraded", "ready": False, "reason": "heartbeat_or_scheduler_progress_missing"}
    process_ready = bool(payload.get("ready")) and payload.get("status") in {"running", "idle"}
    scheduler_ready = bool(payload.get("scheduler_ready"))
    progress_age = scheduler_age
    progress_deadline = 180
    job_progress_active = False
    if payload.get("status") == "running":
        try:
            progress_at = datetime.fromisoformat(str(payload.get("last_job_progress_at")))
            progress_age = max(0, int((now - progress_at).total_seconds()))
            job_progress_active = progress_age <= 900
        except (TypeError, ValueError):
            progress_age = scheduler_age
        # A provider window can legitimately run for several minutes.  The
        # phase timestamp is written by catch_up itself; pulse-only updates do
        # not satisfy this contract.
        progress_deadline = 900
    stale = heartbeat_age > 90 or (progress_age > progress_deadline)
    scheduler_contract_missing = False
    if scheduler_ready:
        try:
            datetime.fromisoformat(str(payload["scheduler_started_at"]))
            datetime.fromisoformat(str(payload["next_expected_run_at"]))
        except (KeyError, TypeError, ValueError):
            scheduler_contract_missing = True
    prolonged = False
    if payload.get("status") == "running" and payload.get("last_job_started_at"):
        try:
            prolonged = (now - datetime.fromisoformat(str(payload["last_job_started_at"]))).total_seconds() > 6 * 60 * 60
        except ValueError:
            prolonged = True
    scheduler_operational = scheduler_ready or job_progress_active
    ready = process_ready and scheduler_operational and not stale and not prolonged and not scheduler_contract_missing
    return {"status": "ok" if ready else "degraded", "ready": ready, "heartbeat_age_seconds": heartbeat_age, "scheduler_age_seconds": scheduler_age, "progress_age_seconds": progress_age, "progress_deadline_seconds": progress_deadline, "stale": stale, "prolonged_job": prolonged, "scheduler_ready": scheduler_ready, "job_progress_active": job_progress_active, "scheduler_contract_missing": scheduler_contract_missing, "heartbeat": payload}


def start_health_server(path: Path, port: int = 8001) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            payload = _read_heartbeat(path)
            result = evaluate_health(payload)
            body = json.dumps({"service": "worker", **result}, ensure_ascii=False).encode()
            self.send_response(200 if result["ready"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    Thread(target=server.serve_forever, daemon=True, name="worker-health").start()
    return server
