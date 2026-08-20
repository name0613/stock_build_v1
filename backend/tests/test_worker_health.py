from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.worker_health import evaluate_health


def _payload(now: datetime) -> dict[str, object]:
    stamp = now.isoformat()
    return {"status": "idle", "ready": True, "scheduler_ready": True, "last_heartbeat_at": stamp, "last_scheduler_heartbeat_at": stamp, "scheduler_started_at": stamp, "next_expected_run_at": (now + timedelta(hours=1)).isoformat()}


def test_health_requires_scheduler_contract_not_only_pulse() -> None:
    now = datetime.now(timezone.utc)
    assert evaluate_health(_payload(now), now)["ready"] is True
    missing = _payload(now)
    missing.pop("next_expected_run_at")
    assert evaluate_health(missing, now)["scheduler_contract_missing"] is True


def test_health_fails_stale_scheduler_and_prolonged_job() -> None:
    now = datetime.now(timezone.utc)
    stale = _payload(now - timedelta(minutes=4))
    assert evaluate_health(stale, now)["ready"] is False
    prolonged = _payload(now)
    prolonged.update({"status": "running", "last_job_started_at": (now - timedelta(hours=7)).isoformat()})
    assert evaluate_health(prolonged, now)["prolonged_job"] is True
    assert evaluate_health(prolonged, now)["ready"] is False
