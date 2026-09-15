from __future__ import annotations

import calendar
from datetime import date, timedelta
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

# Federal non-working public holidays from Article 112 of the Labour Code.
# Regional holidays are intentionally not included: the budget uses the
# all-Russia five-day production calendar.
FEDERAL_HOLIDAYS = {
    (1, 1),
    (1, 2),
    (1, 3),
    (1, 4),
    (1, 5),
    (1, 6),
    (1, 7),
    (1, 8),
    (2, 23),
    (3, 8),
    (5, 1),
    (5, 9),
    (6, 12),
    (11, 4),
}

# Official five-day production-calendar exceptions that are important when
# the remote calendar service is unavailable. Ordinary Saturdays/Sundays and
# federal holidays are handled separately.
#
# 2025: Government Resolution No. 1335 of 04.10.2024.
# 2026: Government Resolution No. 1466 of 24.09.2025 plus the resulting
# observed holiday Mondays (09.03 and 11.05).
# 2027: calendar published by the Ministry of Labour. Keeping an embedded
# copy is especially important around New Year: an incomplete remote calendar
# must never treat 7 January as a working payday.
DAY_OFF_OVERRIDES: dict[int, set[date]] = {
    2025: {
        date(2025, 5, 2),
        date(2025, 5, 8),
        date(2025, 6, 13),
        date(2025, 11, 3),
        date(2025, 12, 31),
    },
    2026: {
        date(2026, 1, 9),
        date(2026, 3, 9),
        date(2026, 5, 11),
        date(2026, 12, 31),
    },
    2027: {
        date(2027, 2, 22),
        date(2027, 5, 3),
        date(2027, 5, 10),
        date(2027, 6, 14),
        date(2027, 11, 5),
        date(2027, 12, 31),
    },
}

WORKDAY_OVERRIDES: dict[int, set[date]] = {
    2025: {date(2025, 11, 1)},
    2026: set(),
    2027: set(),
}


def _known_calendar_status(day: date) -> bool | None:
    """Return official local status for embedded years, otherwise None."""
    if day.year not in DAY_OFF_OVERRIDES:
        return None
    if day in WORKDAY_OVERRIDES.get(day.year, set()):
        return True
    if day in DAY_OFF_OVERRIDES[day.year]:
        return False
    if (day.month, day.day) in FEDERAL_HOLIDAYS:
        return False
    return day.weekday() < 5


@lru_cache(maxsize=16)
def _remote_year(year: int) -> str | None:
    """Load a five-day Russian production calendar from isdayoff.ru.

    The response is one character per calendar day: 0 means working and
    1 means non-working. Other working-day codes are accepted defensively.
    A failed request is not fatal: callers fall back to weekends + federal
    holidays, so the budget remains usable offline.
    """
    url = f"https://isdayoff.ru/api/getdata?year={year}&cc=ru"
    try:
        with urlopen(url, timeout=2.5) as response:  # nosec B310 - fixed trusted host
            payload = response.read().decode("ascii", errors="ignore").strip()
    except (HTTPError, URLError, TimeoutError, OSError):
        return None

    expected = 366 if calendar.isleap(year) else 365
    if len(payload) != expected or any(ch not in "01248" for ch in payload):
        return None
    return payload


def is_working_day_ru(day: date) -> bool:
    """Whether *day* is working in the Russian five-day production calendar."""
    known = _known_calendar_status(day)
    if known is not None:
        return known

    remote = _remote_year(day.year)
    if remote:
        code = remote[day.timetuple().tm_yday - 1]
        return code in {"0", "2", "4"}

    # Calendar for a future year may not have been approved/published yet.
    # This fallback is deliberately conservative and is replaced by the
    # official remote data automatically once it is available after restart.
    if (day.month, day.day) in FEDERAL_HOLIDAYS:
        return False
    return day.weekday() < 5


def payday_on_or_before(nominal: date) -> date:
    """Move a salary/advance date to the latest preceding working day.

    If the nominal date itself is a working day, it is returned unchanged.
    """
    cursor = nominal
    for _ in range(31):
        if is_working_day_ru(cursor):
            return cursor
        cursor -= timedelta(days=1)
    raise ValueError(f"Could not resolve a working day before {nominal.isoformat()}")
