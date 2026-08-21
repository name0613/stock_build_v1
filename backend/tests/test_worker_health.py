from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.worker_health import evaluate_health
from app.worker import _next_scheduled_run_at
from app.calendar import completed_source_end_date, market_session_state, source_publication_window_open
from app.worker import _startup_catch_up_allowed


def _payload(now: datetime) -> dict[str, object]:
    stamp = now.isoformat()
    job_state = {
        job_id: {
            "next_expected_fire_at": (now + timedelta(hours=1)).isoformat(),
            "last_started_fire_at": None,
            "last_completed_fire_at": None,
            "last_event": "REGISTERED",
            "last_event_at": stamp,
            "last_error_code": None,
        }
        for job_id in ("main-sync", "retry-sync")
    }
    return {"status": "idle", "ready": True, "scheduler_ready": True, "last_heartbeat_at": stamp, "last_scheduler_heartbeat_at": stamp, "scheduler_started_at": stamp, "next_expected_run_at": (now + timedelta(hours=1)).isoformat(), "registered_scheduler_job_ids": ["main-sync", "retry-sync"], "scheduler_jobs": job_state}


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


def test_health_detects_due_fire_that_never_started_while_pulse_is_fresh() -> None:
    now = datetime(2026, 8, 21, 14, 40, tzinfo=timezone.utc)
    payload = _payload(now)
    payload["scheduler_jobs"]["main-sync"]["next_expected_fire_at"] = (now - timedelta(minutes=10)).isoformat()  # type: ignore[index]
    result = evaluate_health(payload, now)
    assert result["heartbeat_age_seconds"] == 0
    assert result["scheduler_execution_overdue"] is True
    assert result["ready"] is False
    assert "main-sync:scheduled_fire_not_started" in result["scheduler_job_reasons"]


def test_health_accepts_legitimate_idle_after_scheduled_fire_started_and_completed() -> None:
    now = datetime(2026, 8, 21, 14, 40, tzinfo=timezone.utc)
    payload = _payload(now)
    state = payload["scheduler_jobs"]["main-sync"]  # type: ignore[index]
    state.update({"next_expected_fire_at": (now + timedelta(days=3)).isoformat(), "last_started_fire_at": (now - timedelta(minutes=10)).isoformat(), "last_completed_fire_at": (now - timedelta(minutes=5)).isoformat(), "last_event": "COMPLETED"})
    result = evaluate_health(payload, now)
    assert result["scheduler_execution_overdue"] is False
    assert result["scheduler_event_error"] is False
    assert result["ready"] is True


def test_health_detects_missed_or_error_event_and_stopped_scheduler() -> None:
    now = datetime(2026, 8, 21, 14, 40, tzinfo=timezone.utc)
    missed = _payload(now)
    missed["scheduler_jobs"]["retry-sync"]["last_event"] = "MISSED"  # type: ignore[index]
    assert evaluate_health(missed, now)["scheduler_event_error"] is True
    failed = _payload(now)
    failed["scheduler_jobs"]["main-sync"]["last_event"] = "ERROR"  # type: ignore[index]
    assert evaluate_health(failed, now)["ready"] is False
    stopped = _payload(now)
    stopped["scheduler_ready"] = False
    assert evaluate_health(stopped, now)["ready"] is False


def test_market_session_is_open_only_during_continuous_trading_hours() -> None:
    open_state = market_session_state(datetime(2026, 8, 21, 2, 0, tzinfo=timezone.utc))
    closed_state = market_session_state(datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc))
    holiday_state = market_session_state(datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc))
    assert open_state["state"] == "OPEN"
    assert open_state["monitoring_active"] is True
    assert closed_state["state"] == "CLOSED"
    assert closed_state["monitoring_active"] is False
    assert holiday_state["state"] == "CLOSED"
    assert holiday_state["reason"] == "weekend_or_exchange_holiday"


def test_closed_period_does_not_claim_today_is_a_published_source_date() -> None:
    before_nightly_publication = datetime(2026, 8, 21, 12, 30, tzinfo=timezone.utc)  # 20:30 Taipei
    after_nightly_publication = datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc)  # 21:30 Taipei
    weekend_after_publication = datetime(2026, 8, 22, 3, 30, tzinfo=timezone.utc)  # 11:30 Taipei
    assert completed_source_end_date(before_nightly_publication).isoformat() == "2026-08-20"
    assert completed_source_end_date(after_nightly_publication).isoformat() == "2026-08-21"
    assert completed_source_end_date(weekend_after_publication).isoformat() == "2026-08-21"


def test_worker_does_not_start_provider_catch_up_before_source_publication_window() -> None:
    before_window = datetime(2026, 8, 21, 12, 30, tzinfo=timezone.utc)  # 20:30 Taipei
    after_window = datetime(2026, 8, 21, 13, 30, tzinfo=timezone.utc)  # 21:30 Taipei
    assert source_publication_window_open(before_window) is False
    assert source_publication_window_open(after_window) is True
    assert _startup_catch_up_allowed(before_window) is False
    assert _startup_catch_up_allowed(after_window) is True
