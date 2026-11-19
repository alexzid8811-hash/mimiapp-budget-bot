from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.backup import export_user_data, restore_user_data
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


def buffer_period(client, payday):
    return next(row for row in client.get("/api/buffer").json()["periods"] if row["payday"] == payday)


def test_piggy_vacation_preserves_advance_and_transfers_once(client, monkeypatch):
    advance = buffer_period(client, "2026-09-22")
    baseline_salary = buffer_period(client, "2026-10-07")
    assert client.post("/api/piggy-bank/deposit", json={"amount": 12000}).status_code == 200
    payload = {**vacation_payload("2026-09-21", "2026-09-22", payment_date="2026-09-16"), "reserve_in_piggy": True}
    saved = client.post("/api/vacations", json=payload)
    assert saved.status_code == 200
    vid = saved.json()["id"]
    transfer = f"/api/piggy-bank/vacations/{vid}/transfer"
    part = {"part": "2026-09:salary"}

    before = client.get("/api/piggy-bank").json()
    assert before["balance"] == 12000
    assert before["unassigned"] == 1000
    assert before["vacation_reserves"][0]["reserved"] == 11000
    assert client.post("/api/piggy-bank/to-card", json={"amount": 1000.01}).status_code == 422
    assert client.post(transfer, json=part).status_code == 422
    assert buffer_period(client, "2026-09-22")["to_card"] == advance["to_card"]
    salary = buffer_period(client, "2026-10-07")
    assert salary["payday_amount"] < baseline_salary["payday_amount"]
    assert salary["vacation_included"] == 11000
    assert salary["received"] == round(salary["payday_amount"] + 11000, 2)
    assert salary["vacation_funds"][0]["workdays"] == 2
    assert cashflow_snapshot(1)["current_cash"] == 28000

    monkeypatch.setattr(clock, "today", lambda: date(2026, 10, 7))
    assert buffer_period(client, "2026-10-07")["income_pending"]
    confirmation = {"amount": 42000, "confirmed": True}
    assert client.put("/api/cashflow/income-overrides/2026-10-08", json=confirmation).status_code == 200
    assert client.post(transfer, json=part).status_code == 200
    assert client.post(transfer, json=part).status_code == 200
    row = buffer_period(client, "2026-10-07")
    assert row["income_confirmed"]
    assert row["payday_amount"] == 42000
    assert row["received"] == 53000
    assert row["vacation_funds"][0]["transfer_date"] == "2026-10-07"
    assert client.get("/api/piggy-bank").json()["balance"] == 1000

    backup = export_user_data(1)
    restore_user_data(1, backup)
    row = buffer_period(client, "2026-10-07")
    assert row["received"] == 53000
    assert client.get("/api/piggy-bank").json()["vacation_reserves"] == []


def test_piggy_vacation_splits_to_advance_and_salary_with_exact_kopecks(client):
    assert client.post("/api/piggy-bank/deposit", json={"amount": 11000.01}).status_code == 200
    payload = {**vacation_payload("2026-09-13", "2026-09-17", 11000.01), "reserve_in_piggy": True}
    assert client.post("/api/vacations", json=payload).status_code == 200
    rows = client.get("/api/piggy-bank").json()["vacation_reserves"]
    assert [(row["payday"], row["workdays"], row["reserved"]) for row in rows] == [
        ("2026-09-22", 2, 5500.01),
        ("2026-10-07", 2, 5500.00),
    ]


def test_vacation_reserve_covers_only_money_actually_in_piggy(client):
    deposit = client.post("/api/piggy-bank/deposit", json={"amount": 4000}).json()["movement"]
    payload = {**vacation_payload("2026-09-21", "2026-09-22"), "reserve_in_piggy": True}
    assert client.post("/api/vacations", json=payload).status_code == 200
    piggy = client.get("/api/piggy-bank").json()
    assert piggy["unassigned"] == 0
    assert piggy["vacation_reserves"][0]["amount"] == 11000
    assert piggy["vacation_reserves"][0]["reserved"] == 4000
    assert buffer_period(client, "2026-10-07")["vacation_included"] == 4000
    assert client.delete(f"/api/piggy-bank/movements/{deposit['id']}").status_code == 422
