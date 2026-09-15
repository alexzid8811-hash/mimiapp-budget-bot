from datetime import date, timedelta

import pytest

from app import cashflow_app as flow
from app import main
from app.cashflow import calculate_cashflow_plan
from app.db import connect, ensure_user, init_db


@pytest.fixture
def uid(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "calendar.sqlite3"))
    # Tests must not depend on unpublished calendars or network access.
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda year: None)
    init_db()
    ensure_user(1)
    with connect() as con:
        con.execute("UPDATE income_rules SET amount=23000 WHERE user_id=1")
        con.execute("UPDATE settings SET salary_gross=100000,tax_rate=0 WHERE user_id=1")
    return 1


@pytest.mark.parametrize("payroll_enabled", [0, 1])
@pytest.mark.parametrize("as_of,expected_start,expected_end", [
    (date(2026, 2, 6), date(2026, 2, 6), date(2026, 2, 19)),
    (date(2026, 2, 20), date(2026, 2, 20), date(2026, 3, 5)),
    (date(2025, 12, 30), date(2025, 12, 30), date(2026, 1, 21)),
])
def test_all_calendar_days_between_actual_paydays(
    uid, payroll_enabled, as_of, expected_start, expected_end
):
    with connect() as con:
        con.execute("UPDATE settings SET payroll_enabled=? WHERE user_id=?", (payroll_enabled, uid))
    period = main.current_period(as_of, main.payday_days(uid))
    assert (period.start, period.end) == (expected_start, expected_end)
    days = (expected_end - expected_start).days + 1
    next_payday = expected_end + timedelta(days=1)
    boundaries = flow.payday_boundaries(uid, expected_start, next_payday)
    assert [b["date"] for b in boundaries] == [expected_start, next_payday]

    # Income belongs to the actual date, not a second nominal-date occurrence.
    income = flow.planned_income_map(uid, expected_start, expected_end)
    assert list(income) == [expected_start]
    assert main.period_recurring_income(uid, expected_start, expected_end) == sum(income.values())

    plan = calculate_cashflow_plan(
        today=expected_start, horizon_end=expected_end,
        opening_balance_before_today_spend=days * 1000,
        spent_today=0, income_by_date={}, mandatory_by_date={},
    )
    assert plan.daily_target == 1000
    assert plan.projected_end_balance == 0
    rows = flow.cashflow_period_rows(
        uid, today=expected_start, horizon_end=expected_end,
        opening_balance_before_today_spend=days * 1000, daily_target=plan.daily_target,
    )
    assert len(rows) == 1
    assert rows[0]["days"] == days
    assert rows[0]["buffer"] == 0
    assert rows[0]["daily"] == 1000


def test_salary_kind_is_a_boundary_even_without_payday_flag(uid):
    with connect() as con:
        con.execute("UPDATE income_rules SET is_payday=0,day_of_month=10 WHERE kind='salary'")
        con.execute("UPDATE income_rules SET is_payday=0,day_of_month=25 WHERE kind='advance'")
    assert main.payday_days(uid) == [10, 25]


def test_other_income_and_bills_are_not_shifted(uid):
    with connect() as con:
        con.execute(
            "INSERT INTO income_rules(user_id,title,amount,day_of_month,kind) "
            "VALUES(1,'Другой доход',500,7,'other')"
        )
        con.execute(
            "INSERT INTO bill_rules(user_id,title,amount,day_of_month) VALUES(1,'Платёж',300,7)"
        )
    assert main.period_recurring_income(uid, date(2026, 2, 6), date(2026, 2, 6)) == 23000
    assert main.period_recurring_income(uid, date(2026, 2, 7), date(2026, 2, 7)) == 500
    assert flow.planned_mandatory_map(uid, date(2026, 2, 6), date(2026, 2, 7)) == {
        date(2026, 2, 7): 300,
    }


def test_transferred_working_saturday_is_not_skipped(uid):
    with connect() as con:
        con.execute("UPDATE income_rules SET day_of_month=3 WHERE kind='salary'")
    events = flow.payday_boundaries(uid, date(2025, 10, 31), date(2025, 11, 4))
    assert [event["date"] for event in events] == [date(2025, 11, 1)]
    assert main.period_recurring_income(uid, date(2025, 11, 1), date(2025, 11, 1)) == 23000


def test_january_income_is_booked_in_december_once(uid):
    assert main.period_recurring_income(uid, date(2025, 12, 30), date(2025, 12, 31)) == 23000
    assert main.period_recurring_income(uid, date(2026, 1, 1), date(2026, 1, 21)) == 0


@pytest.mark.parametrize("payroll_enabled", [0, 1])
def test_snapshot_uses_every_day_until_shifted_next_payment(uid, monkeypatch, payroll_enabled):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 2, 20)

    monkeypatch.setattr(flow, "date", FixedDate)
    with connect() as con:
        con.execute(
            "UPDATE settings SET cashflow_enabled=1,cashflow_start_date='2026-02-20',"
            "initial_reserve=10000,forecast_months=1,payroll_enabled=? WHERE user_id=?",
            (payroll_enabled, uid),
        )
    snapshot = flow.cashflow_snapshot(uid)
    first = snapshot["periods"][0]
    assert first["start"] == "2026-02-20"
    assert first["end"] == "2026-03-05"
    assert first["days"] == 14  # Includes weekends and 23 February.
    assert snapshot["next_income"]["date"] == "2026-03-06"
    assert snapshot["remaining_period"] == round(snapshot["daily_target"] * 14, 2)
