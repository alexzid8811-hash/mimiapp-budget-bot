from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.cashflow_app import app, cashflow_snapshot
from app.db import connect, ensure_user, init_db
from app.payroll import PayrollConfig, payroll_for_accrual_month


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 16))
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    init_db()
    ensure_user(1)
    with TestClient(app) as client:
        assert client.put("/api/payroll-settings", json={
            "payroll_enabled": True, "salary_gross": 100000,
        }).status_code == 200
        assert client.put("/api/cashflow-settings", json={
            "cashflow_enabled": True, "start_date": "2026-09-15", "start_capital": 28000,
        }).status_code == 200
        yield client


def vacation_payload(start="2026-09-07", end="2026-09-08", amount=11000, payment_date="2026-09-04"):
    return {"start_date": start, "end_date": end, "amount": amount, "payment_date": payment_date}


def test_vacation_pay_is_separate_income_that_does_not_double_count(client):
    payroll_before = client.get("/api/payroll-settings").json()
    created = client.post("/api/vacations", json=vacation_payload())
    assert created.status_code == 200
    vacation_id = created.json()["id"]

    flow = cashflow_snapshot(1)
    assert flow["current_cash"] == 28000
    payroll_after = client.get("/api/payroll-settings").json()
    assert payroll_after["settings"] == payroll_before["settings"]
    assert payroll_after["preview"]["advance"] < payroll_before["preview"]["advance"]
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    updated = client.put(f"/api/vacations/{vacation_id}", json=vacation_payload(amount=9000))
    assert updated.status_code == 200
    assert cashflow_snapshot(1)["current_cash"] == 28000

    assert client.delete(f"/api/vacations/{vacation_id}").status_code == 200
    assert cashflow_snapshot(1)["current_cash"] == 28000


def test_future_vacation_pay_requires_actual_receipt_after_payment_day(client, monkeypatch):
    created = client.post("/api/vacations", json=vacation_payload(payment_date="2026-09-20"))
    assert created.status_code == 200
    flow = cashflow_snapshot(1)
    assert flow["current_cash"] == 28000  # still only a forecast

    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 20))
    flow = cashflow_snapshot(1)
    assert flow["current_cash"] == 28000  # vacation pay is not received automatically
    assert client.post("/api/transactions", json={
        "type": "income", "amount": 11000, "tx_date": "2026-09-20", "note": "Отпускные",
    }).status_code == 200
    assert cashflow_snapshot(1)["current_cash"] == 39000


def test_old_vacation_and_obsolete_manual_column_are_not_added(client):
    assert client.post("/api/vacations", json=vacation_payload(payment_date="2026-08-01")).status_code == 200
    with connect() as con:
        con.execute("UPDATE settings SET initial_vacation_reserve=99999 WHERE user_id=1")
    flow = cashflow_snapshot(1)
    assert flow["current_cash"] == 28000


def test_vacation_in_first_half_of_month_reduces_only_the_advance():
    # 7-8 September: working days of the first half, so only the advance
    # (accrued 1-15) drops; the September salary (16-30) is unaffected.
    config = PayrollConfig(salary_gross=100000, bonus_gross=0, tax_rate=0)
    without = payroll_for_accrual_month(2026, 9, config, [])
    with_vacation = payroll_for_accrual_month(
        2026, 9, config, [{"start_date": "2026-09-07", "end_date": "2026-09-08", "id": 1}]
    )
    assert with_vacation["advance"] < without["advance"]
    assert with_vacation["worked_days_first_half"] == without["workdays_first_half"] - 2
    # The actual salary payment (7 October) is unaffected: only the advance,
    # the payment the vacation days actually belong to, changes.
    assert with_vacation["final_salary"] == without["final_salary"]


def test_vacation_spanning_the_15th_splits_between_advance_and_salary():
    # 13-17 September: 13-15 (Sun/Mon/Tue) are worked days of the first half,
    # 16-17 (Wed/Thu) are worked days of the second half. 13 Sep is a Sunday,
    # so only 14-15 are working days in the first half here.
    config = PayrollConfig(salary_gross=100000, bonus_gross=0, tax_rate=0)
    without = payroll_for_accrual_month(2026, 9, config, [])
    with_vacation = payroll_for_accrual_month(
        2026, 9, config, [{"start_date": "2026-09-13", "end_date": "2026-09-17", "id": 1}]
    )
    first_half_vacation_days = with_vacation["workdays_first_half"] - with_vacation["worked_days_first_half"]
    second_half_vacation_days = with_vacation["vacation_workdays"] - first_half_vacation_days
    assert first_half_vacation_days == 2   # 14, 15 September
    assert second_half_vacation_days == 2  # 16, 17 September
    assert with_vacation["advance"] < without["advance"]
    assert with_vacation["salary_net_for_worked_days"] < without["salary_net_for_worked_days"]


def test_vacation_in_second_half_does_not_touch_already_paid_advance():
    # A vacation starting after the 15th reduces only the salary; the advance
    # (already computed from days 1-15) is untouched.
    config = PayrollConfig(salary_gross=100000, bonus_gross=0, tax_rate=0)
    without = payroll_for_accrual_month(2026, 9, config, [])
    with_vacation = payroll_for_accrual_month(
        2026, 9, config, [{"start_date": "2026-09-21", "end_date": "2026-09-22", "id": 1}]
    )
    assert with_vacation["advance"] == without["advance"]
    assert with_vacation["salary_net_for_worked_days"] < without["salary_net_for_worked_days"]
