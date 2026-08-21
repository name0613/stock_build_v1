from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.worker_health import evaluate_health
from app.worker import _next_scheduled_run_at


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


def test_running_job_requires_actual_phase_progress_not_only_process_pulse() -> None:
    now = datetime.now(timezone.utc)
    running = _payload(now)
    running.update({"status": "running", "scheduler_ready": False, "last_job_started_at": (now - timedelta(minutes=16)).isoformat(), "last_job_progress_at": (now - timedelta(minutes=16)).isoformat()})
    assert evaluate_health(running, now)["ready"] is False
    assert evaluate_health(running, now)["stale"] is True
    running["last_job_progress_at"] = (now - timedelta(seconds=30)).isoformat()
    assert evaluate_health(running, now)["ready"] is True


def test_idle_health_is_degraded_when_scheduler_heartbeat_misses_180_seconds() -> None:
    now = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
    payload = _payload(now - timedelta(seconds=181))
    assert evaluate_health(payload, now)["scheduler_age_seconds"] >= 181
    assert evaluate_health(payload, now)["ready"] is False


def test_next_run_includes_main_and_retry_schedule() -> None:
    # 22:00 Taipei on Friday should select the same day's 23:00 retry.
    now = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)
    next_run = datetime.fromisoformat(_next_scheduled_run_at(now))
    assert next_run == datetime(2026, 8, 21, 15, 0, tzinfo=timezone.utc)
