from datetime import date

from app.budget import current_period, dashboard_numbers, occurrences, reserve_needed_for_future
from app.russian_calendar import payday_on_or_before


def test_period_starts_day_after_payday():
    p = current_period(date(2026, 9, 14), [7, 22])
    assert p.start == date(2026, 9, 8)
    assert p.end == date(2026, 9, 22)


def test_next_period_starts_day_after_advance():
    p = current_period(date(2026, 9, 29), [7, 22])
    assert p.start == date(2026, 9, 23)
    assert p.end == date(2026, 10, 7)


def test_salary_and_advance_move_before_weekend():
    assert payday_on_or_before(date(2026, 2, 7)) == date(2026, 2, 6)
    assert payday_on_or_before(date(2026, 2, 22)) == date(2026, 2, 20)


def test_period_boundaries_use_actual_working_paydays():
    p = current_period(date(2026, 2, 8), [7, 22])
    assert p.start == date(2026, 2, 7)
    assert p.end == date(2026, 2, 20)


def test_january_salary_can_move_into_previous_month():
    # 31 Dec 2025 is non-working and 1-8 Jan are New Year holidays.
    assert payday_on_or_before(date(2026, 1, 7)) == date(2025, 12, 30)
    dates = occurrences(
        [7],
        date(2025, 12, 29),
        date(2026, 1, 10),
        move_to_previous_workday=True,
    )
    assert date(2025, 12, 30) in dates


def test_2027_new_year_holidays_move_january_salary_to_december(monkeypatch):
    # The embedded calendar must win even if an external service incorrectly
    # says every weekday in an upcoming year is working.
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda year: "0" * 365)
    assert payday_on_or_before(date(2027, 1, 7)) == date(2026, 12, 30)
    period = current_period(date(2027, 1, 7), [7, 22])
    assert period.start == date(2026, 12, 31)
    assert period.end == date(2027, 1, 22)


def test_2027_february_weekend_moves_salary_before_weekend(monkeypatch):
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda year: "0" * 365)
    assert payday_on_or_before(date(2027, 2, 7)) == date(2027, 2, 5)
    period = current_period(date(2027, 2, 1), [7, 22])
    assert period.start == date(2027, 1, 23)
    assert period.end == date(2027, 2, 5)


def test_reserve_for_future_deficit():
    assert reserve_needed_for_future([5000, -12000, 2000]) == 7000
    assert reserve_needed_for_future([5000, 1000, -2000]) == 0


def test_daily_recalculates_after_overspend():
    before = dashboard_numbers(100000, 36000, 0, 5000, 0, 15)
    after = dashboard_numbers(100000, 36000, 5000, 5000, 0, 14)
    assert before["period_budget"] == 59000
    assert after["remaining"] == 54000
    assert round(after["daily"], 2) == 3857.14
