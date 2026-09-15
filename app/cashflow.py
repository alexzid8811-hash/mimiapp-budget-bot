from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .money import amount, cents


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
    if horizon_end < today:
        raise ValueError("Horizon ends before today")
    opening = cents(opening_balance_before_today_spend)
    spent = max(0, cents(spent_today))

    resources = opening
    minimum_without_daily_spend = opening - spent
    daily_target: int | None = None
    fixed_rows: list[tuple[date, int, int]] = []

    for index, day in enumerate(daterange(today, horizon_end), start=1):
        # Today's fixed flows are already included in the opening balance.
        income = 0 if day == today else cents(income_by_date.get(day, 0))
        mandatory = 0 if day == today else cents(mandatory_by_date.get(day, 0))
        resources += income - mandatory
        minimum_without_daily_spend = min(minimum_without_daily_spend, resources - spent)
        # max(already_spent, daily) + (index - 1) * daily <= resources.
        # Both bounds are needed after an overspend; integer division never
        # promises a fraction of a kopeck that the account cannot cover.
        candidate = resources // index
        if index > 1:
            candidate = min(candidate, (resources - spent) // (index - 1))
        daily_target = candidate if daily_target is None else min(daily_target, candidate)
        fixed_rows.append((day, income, mandatory))

    safe_daily = max(0, daily_target or 0)
    available_today = max(0, safe_daily - spent)
    capital_shortfall = max(0, -minimum_without_daily_spend)

    # Simulate the plan. The money left after today's safe spending is the
    # virtual buffer reserved for future days and obligatory payments.
    balance = opening
    minimum_balance = opening - spent
    timeline: list[dict] = []
    for day, income, mandatory in fixed_rows:
        if day != today:
            balance += income - mandatory
        balance -= max(spent, safe_daily) if day == today else safe_daily
        minimum_balance = min(minimum_balance, balance)
        if income or mandatory:
            timeline.append(
                {
                    "date": day.isoformat(),
                    "income": amount(income),
                    "mandatory": amount(mandatory),
                    "buffer_after_day": amount(max(0, balance)),
                }
            )

    buffer_balance = max(0, opening - max(spent, safe_daily))
    return CashflowPlan(
        daily_target=amount(safe_daily),
        available_today=amount(available_today),
        buffer_balance=amount(buffer_balance),
        capital_shortfall=amount(capital_shortfall),
        projected_end_balance=amount(balance),
        minimum_projected_balance=amount(minimum_balance),
        timeline=timeline,
    )
