from datetime import date

from app.budget import current_period, dashboard_numbers, reserve_needed_for_future


def test_period_7_to_21():
    p = current_period(date(2026, 9, 14), [7, 22])
    assert p.start == date(2026, 9, 7)
    assert p.end == date(2026, 9, 21)


def test_period_22_to_6_next_month():
    p = current_period(date(2026, 9, 29), [7, 22])
    assert p.start == date(2026, 9, 22)
    assert p.end == date(2026, 10, 6)


def test_reserve_for_future_deficit():
    assert reserve_needed_for_future([5000, -12000, 2000]) == 7000
    assert reserve_needed_for_future([5000, 1000, -2000]) == 0


def test_daily_recalculates_after_overspend():
    before = dashboard_numbers(100000, 36000, 0, 5000, 0, 15)
    after = dashboard_numbers(100000, 36000, 5000, 5000, 0, 14)
    assert before["period_budget"] == 59000
    assert after["remaining"] == 54000
    assert round(after["daily"], 2) == 3857.14
