from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable


@dataclass(frozen=True)
class Period:
    start: date
    end: date


def month_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def add_months(d: date, months: int) -> date:
    idx = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(idx, 12)
    return date(year, month0 + 1, min(d.day, calendar.monthrange(year, month0 + 1)[1]))


def occurrences(days: Iterable[int], start: date, end: date) -> list[date]:
    result: set[date] = set()
    cursor = date(start.year, start.month, 1)
    last_month = date(end.year, end.month, 1)
    while cursor <= last_month:
        for day in days:
            d = month_date(cursor.year, cursor.month, int(day))
            if start <= d <= end:
                result.add(d)
        cursor = add_months(cursor, 1).replace(day=1)
    return sorted(result)


def current_period(as_of: date, payday_days: list[int]) -> Period:
    if not payday_days:
        payday_days = [1]
    search_start = add_months(as_of.replace(day=1), -2)
    search_end = add_months(as_of.replace(day=1), 2) + timedelta(days=40)
    dates = occurrences(payday_days, search_start, search_end)
    previous = [d for d in dates if d <= as_of]
    future = [d for d in dates if d > as_of]
    if not previous or not future:
        raise ValueError("Could not resolve payday period")
    start = previous[-1]
    next_start = future[0]
    return Period(start=start, end=next_start - timedelta(days=1))


def period_sequence(after: date, payday_days: list[int], count: int) -> list[Period]:
    search_start = after - timedelta(days=1)
    search_end = add_months(after, max(3, count + 2)) + timedelta(days=40)
    dates = occurrences(payday_days, search_start, search_end)
    dates = [d for d in dates if d >= after]
    result: list[Period] = []
    for i in range(min(count, max(0, len(dates) - 1))):
        result.append(Period(dates[i], dates[i + 1] - timedelta(days=1)))
    return result


def reserve_needed_for_future(period_nets: list[float]) -> float:
    """Minimum reserve required now so cumulative future structural cash-flow never drops below zero."""
    running = 0.0
    minimum = 0.0
    for net in period_nets:
        running += net
        minimum = min(minimum, running)
    return round(max(0.0, -minimum), 2)


def dashboard_numbers(
    period_income: float,
    mandatory: float,
    discretionary_spent: float,
    reserve_in: float,
    reserve_out: float,
    remaining_days: int,
) -> dict[str, float]:
    period_budget = max(0.0, period_income + reserve_out - mandatory - reserve_in)
    remaining = max(0.0, period_budget - discretionary_spent)
    daily = remaining / max(1, remaining_days)
    return {
        "period_budget": round(period_budget, 2),
        "remaining": round(remaining, 2),
        "daily": round(daily, 2),
    }
