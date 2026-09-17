from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.cashflow_app import app, cashflow_snapshot
from app.db import connect, ensure_user, init_db


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


def vacation_payload(amount=11000, payment_date="2026-09-11"):
    return {
        "start_date": "2026-09-07", "end_date": "2026-09-08",
        "amount": amount, "payment_date": payment_date,
    }


def test_vacation_changes_payroll_but_is_not_double_counted_as_cash(client):
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
    assert sum(row["income"] for row in flow["timeline"] if row["date"] == "2026-09-20") >= 11000

    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 20))
    flow = cashflow_snapshot(1)
    assert flow["current_cash"] == 28000
    assert not any(row["date"] == "2026-09-20" and row["income"] >= 11000 for row in flow["timeline"])
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
