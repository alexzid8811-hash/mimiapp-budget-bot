from datetime import date

from app.budget import current_period, occurrences
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


def test_2027_working_days_match_the_official_calendar(monkeypatch):
    # Production calendar 2027 (Resolution No. 1187 of 17.09.2026): working
    # days per month for the five-day week, 247 in the year.
    import calendar
    from app.russian_calendar import is_working_day_ru as is_workday

    monkeypatch.setattr("app.russian_calendar._remote_year", lambda year: "0" * 365)
    counts = [
        sum(is_workday(date(2027, m, d)) for d in range(1, calendar.monthrange(2027, m)[1] + 1))
        for m in range(1, 13)
    ]
    assert counts == [15, 19, 22, 22, 19, 21, 22, 22, 22, 21, 20, 22]
    # Saturday 20 February is a working day: the 22nd advance comes on it.
    assert payday_on_or_before(date(2027, 2, 22)) == date(2027, 2, 20)
