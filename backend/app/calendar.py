from __future__ import annotations

from datetime import date, timedelta

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


def is_trading_session(day: date, holidays: set[date] | None = None) -> bool:
    return day.weekday() < 5 and day not in (holidays or TW_HOLIDAYS)


def expected_trading_sessions(end: date, count: int, holidays: set[date] | None = None) -> list[date]:
    sessions: list[date] = []
    cursor = end
    while len(sessions) < count:
        if is_trading_session(cursor, holidays):
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(sessions))


def missing_sessions(observed: list[date], end: date, count: int) -> list[str]:
    expected = expected_trading_sessions(end, count)
    observed_set = set(observed)
    return [day.isoformat() for day in expected if day not in observed_set]

