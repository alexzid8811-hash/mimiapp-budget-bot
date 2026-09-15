from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class CashflowPlan:
    daily_target: float
    available_today: float
    buffer_balance: float
    capital_shortfall: float
    projected_end_balance: float
    minimum_projected_balance: float
    timeline: list[dict]


def daterange(start: date, end: date):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def calculate_cashflow_plan(
    *,
    today: date,
    horizon_end: date,
    opening_balance_before_today_spend: float,
    spent_today: float,
    income_by_date: dict[date, float],
    mandatory_by_date: dict[date, float],
) -> CashflowPlan:
    """Calculate the maximum constant daily spending amount that never makes cash negative.

    ``opening_balance_before_today_spend`` already includes every fixed cash-flow
    through today but adds today's discretionary spending back. This lets the
    returned ``daily_target`` represent the full safe amount for today, while
    ``available_today`` is the remaining part after today's real spending.
    """
    opening = float(opening_balance_before_today_spend)
    spent_today = max(0.0, float(spent_today))

    resources = opening
    minimum_without_daily_spend = opening
    daily_target: float | None = None
    fixed_rows: list[tuple[date, float, float]] = []

    for index, day in enumerate(daterange(today, horizon_end), start=1):
        # Today's fixed flows are already included in the opening balance.
        income = 0.0 if day == today else float(income_by_date.get(day, 0.0))
        mandatory = 0.0 if day == today else float(mandatory_by_date.get(day, 0.0))
        resources += income - mandatory
        minimum_without_daily_spend = min(minimum_without_daily_spend, resources)
        candidate = resources / index
        daily_target = candidate if daily_target is None else min(daily_target, candidate)
        fixed_rows.append((day, income, mandatory))

    safe_daily = round(max(0.0, daily_target or 0.0), 2)
    available_today = round(max(0.0, safe_daily - spent_today), 2)
    capital_shortfall = round(max(0.0, -minimum_without_daily_spend), 2)

    # Simulate the plan. The money left after today's safe spending is the
    # virtual buffer reserved for future days and obligatory payments.
    balance = opening
    minimum_balance = opening
    timeline: list[dict] = []
    for day, income, mandatory in fixed_rows:
        if day != today:
            balance += income - mandatory
        balance -= safe_daily
        minimum_balance = min(minimum_balance, balance)
        if income or mandatory:
            timeline.append(
                {
                    "date": day.isoformat(),
                    "income": round(income, 2),
                    "mandatory": round(mandatory, 2),
                    "buffer_after_day": round(max(0.0, balance), 2),
                }
            )

    buffer_balance = round(max(0.0, opening - safe_daily), 2)
    return CashflowPlan(
        daily_target=safe_daily,
        available_today=available_today,
        buffer_balance=buffer_balance,
        capital_shortfall=capital_shortfall,
        projected_end_balance=round(balance, 2),
        minimum_projected_balance=round(minimum_balance, 2),
        timeline=timeline,
    )
