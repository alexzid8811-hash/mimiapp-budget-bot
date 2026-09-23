from datetime import date, timedelta

import pytest

from app import cashflow_app as flow
from app import engine, planning
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


def paydays(uid, start, end):
    return sorted(engine.payday_schedule(uid, start, end)["paydays"])


@pytest.mark.parametrize("payroll_enabled", [0, 1])
@pytest.mark.parametrize("as_of,expected_start,expected_end", [
    # 6 February is the real salary date, so the previous card period still
    # covers that day; the salary is spendable from 7 February.
    (date(2026, 2, 6), date(2026, 1, 23), date(2026, 2, 6)),
    (date(2026, 2, 7), date(2026, 2, 7), date(2026, 2, 20)),
    (date(2026, 2, 21), date(2026, 2, 21), date(2026, 3, 6)),
    (date(2025, 12, 31), date(2025, 12, 31), date(2026, 1, 22)),
])
def test_card_period_runs_to_and_including_the_actual_payday(
    uid, payroll_enabled, as_of, expected_start, expected_end
):
    with connect() as con:
        con.execute("UPDATE settings SET payroll_enabled=? WHERE user_id=?", (payroll_enabled, uid))
    period = planning.user_period(uid, as_of)
    assert (period.start, period.end) == (expected_start, expected_end)
    # Buffer periods begin on the payday itself.
    assert expected_end in paydays(uid, expected_start, expected_end)
    assert expected_start - timedelta(days=1) in paydays(uid, expected_start - timedelta(days=1), expected_end)


def test_salary_kind_is_a_boundary_even_without_payday_flag(uid):
    with connect() as con:
        con.execute("UPDATE income_rules SET is_payday=0,day_of_month=10 WHERE kind='salary'")
        con.execute("UPDATE income_rules SET is_payday=0,day_of_month=25 WHERE kind='advance'")
    assert [d.day for d in paydays(uid, date(2026, 3, 1), date(2026, 3, 31))] == [10, 25]


def test_other_income_and_bills_are_not_shifted(uid, monkeypatch):
    monkeypatch.setattr("app.clock.today", lambda: date(2026, 2, 5))
    with connect() as con:
        con.execute(
            "INSERT INTO income_rules(user_id,title,amount,day_of_month,kind) "
            "VALUES(1,'Другой доход',500,7,'other')"
        )
        con.execute(
            "INSERT INTO bill_rules(user_id,title,amount,day_of_month) VALUES(1,'Платёж',300,7)"
        )
    inp, _ = engine.build_input(uid, start=date(2026, 2, 1), capital=0, months=1, today=date(2026, 2, 5))
    # Salary moves from Saturday 7 to Friday 6; the other income and the bill
    # stay on their own dates.
    assert inp.buffer_in[date(2026, 2, 6)] == 2300000
    assert inp.buffer_in[date(2026, 2, 7)] == 50000
    assert inp.bills_out[date(2026, 2, 7)] == 30000


def test_transferred_working_saturday_is_not_skipped(uid):
    with connect() as con:
        con.execute("UPDATE income_rules SET day_of_month=3 WHERE kind='salary'")
    # 1 November 2025 is a working Saturday, so the 3rd moves to the 1st.
    assert date(2025, 11, 1) in paydays(uid, date(2025, 10, 31), date(2025, 11, 4))
    events = planning.payday_boundaries(uid, date(2025, 10, 31), date(2025, 11, 4))
    assert [event["date"] for event in events] == [date(2025, 11, 2)]


def test_january_income_is_booked_in_december_once(uid):
    january = [d for d in paydays(uid, date(2025, 12, 1), date(2026, 1, 31)) if d.day in (7, 30)]
    assert date(2025, 12, 30) in january
    assert date(2026, 1, 7) not in january


@pytest.mark.parametrize("payroll_enabled", [0, 1])
def test_snapshot_uses_every_day_until_shifted_next_payment(uid, monkeypatch, payroll_enabled):
    monkeypatch.setattr("app.clock.today", lambda: date(2026, 2, 21))
    with connect() as con:
        con.execute(
            "UPDATE settings SET cashflow_enabled=1,cashflow_start_date='2026-02-20',"
            "cashflow_start_capital=10000,forecast_months=1,payroll_enabled=? WHERE user_id=?",
            (payroll_enabled, uid),
        )
    snapshot = flow.cashflow_snapshot(uid)
    period = snapshot["period"]
    assert (period["start"], period["end"]) == ("2026-02-21", "2026-03-06")
    assert period["days"] == 14  # Includes weekends and 23 February.
    first = snapshot["periods"][0]
    # The buffer period of the 20 February advance ends the day before salary.
    assert (first["start"], first["end"]) == ("2026-02-20", "2026-03-05")
    assert snapshot["next_income"]["date"] == "2026-03-06"
    assert snapshot["remaining_period"] >= round(snapshot["daily_target"] * 14, 2)
