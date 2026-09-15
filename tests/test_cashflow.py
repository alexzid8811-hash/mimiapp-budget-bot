from datetime import date, timedelta

from app.cashflow import calculate_cashflow_plan


def test_safe_daily_spreads_start_capital_across_horizon():
    today = date(2026, 9, 15)
    plan = calculate_cashflow_plan(
        today=today,
        horizon_end=today + timedelta(days=9),
        opening_balance_before_today_spend=1000,
        spent_today=0,
        income_by_date={},
        mandatory_by_date={},
    )
    assert plan.daily_target == 100
    assert plan.available_today == 100
    assert plan.buffer_balance == 900


def test_upcoming_bill_reduces_daily_limit_before_bill_date():
    today = date(2026, 9, 15)
    plan = calculate_cashflow_plan(
        today=today,
        horizon_end=today + timedelta(days=4),
        opening_balance_before_today_spend=600,
        spent_today=0,
        income_by_date={today + timedelta(days=4): 1000},
        mandatory_by_date={today + timedelta(days=2): 500},
    )
    assert plan.daily_target == 25
    assert plan.capital_shortfall == 0


def test_structural_shortfall_is_reported_even_with_zero_daily_spend():
    today = date(2026, 9, 15)
    plan = calculate_cashflow_plan(
        today=today,
        horizon_end=today + timedelta(days=2),
        opening_balance_before_today_spend=100,
        spent_today=0,
        income_by_date={},
        mandatory_by_date={today + timedelta(days=1): 500},
    )
    assert plan.daily_target == 0
    assert plan.capital_shortfall == 400


def test_spent_today_reduces_only_remaining_today():
    today = date(2026, 9, 15)
    plan = calculate_cashflow_plan(
        today=today,
        horizon_end=today + timedelta(days=9),
        opening_balance_before_today_spend=1000,
        spent_today=60,
        income_by_date={},
        mandatory_by_date={},
    )
    assert plan.daily_target == 100
    assert plan.available_today == 40
