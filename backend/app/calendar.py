from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import json
from zoneinfo import ZoneInfo

CALENDAR_VERSION = "tw-exchange-2026-v1"
CALENDAR_COVERAGE_START = date(2026, 1, 1)
CALENDAR_COVERAGE_END = date(2026, 12, 31)


class CalendarUnknownError(ValueError):
    """The versioned exchange calendar does not cover a requested date."""

# Taiwan exchange holidays used by the rolling-window validator.  The list is
# deliberately explicit and reviewable; an unknown future holiday is treated
# as an expected session until the next release adds it, never silently
# backfilled with an older observation.
TW_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 28), date(2026, 4, 3),
    date(2026, 4, 6), date(2026, 5, 1), date(2026, 6, 19), date(2026, 9, 25),
    date(2026, 10, 9), date(2026, 10, 26), date(2026, 12, 25),
}
CALENDAR_MANIFEST = {"version": CALENDAR_VERSION, "coverage_start": CALENDAR_COVERAGE_START.isoformat(), "coverage_end": CALENDAR_COVERAGE_END.isoformat(), "holidays": sorted(day.isoformat() for day in TW_HOLIDAYS)}
CALENDAR_HASH = hashlib.sha256(json.dumps(CALENDAR_MANIFEST, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
MARKET_TIMEZONE = "Asia/Taipei"
MARKET_OPEN_TIME = time(9, 0)
MARKET_CLOSE_TIME = time(13, 30)
SOURCE_DATA_READY_TIME = time(21, 0)


def is_trading_session(day: date, holidays: set[date] | None = None) -> bool:
    if holidays is None and not CALENDAR_COVERAGE_START <= day <= CALENDAR_COVERAGE_END:
        raise CalendarUnknownError(f"calendar coverage unknown for {day.isoformat()}")
    return day.weekday() < 5 and day not in (holidays or TW_HOLIDAYS)


def market_session_state(now: datetime | None = None) -> dict[str, object]:
    """Describe whether continuous Taiwan equity trading is currently open.

    This is deliberately separate from source-publication scheduling.  FinMind
    daily observations commonly arrive after the exchange closes, so a CLOSED
    state is expected and must not be treated as a provider failure.
    """
    current = (now or datetime.now(ZoneInfo(MARKET_TIMEZONE))).astimezone(ZoneInfo(MARKET_TIMEZONE))
    try:
        trading_day = is_trading_session(current.date())
    except CalendarUnknownError:
        return {
            "state": "UNKNOWN",
            "monitoring_active": False,
            "reason": "calendar_coverage_unknown",
            "timezone": MARKET_TIMEZONE,
            "local_date": current.date().isoformat(),
            "local_time": current.time().replace(microsecond=0).isoformat(),
            "open_time": MARKET_OPEN_TIME.isoformat(),
            "close_time": MARKET_CLOSE_TIME.isoformat(),
            "calendar_version": CALENDAR_VERSION,
            "calendar_hash": CALENDAR_HASH,
        }
    if not trading_day:
        reason = "weekend_or_exchange_holiday"
        is_open = False
    else:
        current_time = current.time().replace(tzinfo=None)
        is_open = MARKET_OPEN_TIME <= current_time < MARKET_CLOSE_TIME
        reason = "continuous_trading" if is_open else "outside_continuous_trading_hours"
    return {
        "state": "OPEN" if is_open else "CLOSED",
        "monitoring_active": is_open,
        "reason": reason,
        "timezone": MARKET_TIMEZONE,
        "local_date": current.date().isoformat(),
        "local_time": current.time().replace(microsecond=0).isoformat(),
        "open_time": MARKET_OPEN_TIME.isoformat(),
        "close_time": MARKET_CLOSE_TIME.isoformat(),
        "calendar_version": CALENDAR_VERSION,
        "calendar_hash": CALENDAR_HASH,
    }


def completed_source_end_date(now: datetime | None = None) -> date:
    """Return the latest source date that the nightly provider cycle may use.

    Daily FinMind observations are consumed after the exchange session and
    provider publication window.  During the closed period before the
    nightly cycle, today's trading session is therefore not yet an eligible
    source target.  This is intentionally separate from market_session_state:
    OPEN controls live monitoring semantics, while this cutoff controls batch
    source freshness and prevents a closed-market run from claiming today's
    data is available.
    """
    current = (now or datetime.now(ZoneInfo(MARKET_TIMEZONE))).astimezone(ZoneInfo(MARKET_TIMEZONE))
    candidate = current.date() if current.time().replace(tzinfo=None) >= SOURCE_DATA_READY_TIME else current.date() - timedelta(days=1)
    return expected_trading_sessions(candidate, 1)[-1]


def expected_trading_sessions(end: date, count: int, holidays: set[date] | None = None) -> list[date]:
    if holidays is None and not CALENDAR_COVERAGE_START <= end <= CALENDAR_COVERAGE_END:
        raise CalendarUnknownError(f"calendar coverage unknown for {end.isoformat()}")
    sessions: list[date] = []
    cursor = end
    while len(sessions) < count:
        if is_trading_session(cursor, holidays):
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(sessions))


def calendar_snapshot() -> dict[str, object]:
    return {**CALENDAR_MANIFEST, "holiday_count": len(TW_HOLIDAYS), "calendar_hash": CALENDAR_HASH}


def missing_sessions(observed: list[date], end: date, count: int) -> list[str]:
    expected = expected_trading_sessions(end, count)
    observed_set = set(observed)
    return [day.isoformat() for day in expected if day not in observed_set]
