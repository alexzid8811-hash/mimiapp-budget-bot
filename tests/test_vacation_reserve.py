from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.backup import export_user_data, restore_user_data
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
    ensure_user(2)
    with TestClient(app) as client:
        assert client.put("/api/cashflow-settings", json={
            "cashflow_enabled": True, "start_date": "2026-09-15", "start_capital": 28000,
        }).status_code == 200
        yield client


def test_opening_vacation_money_counted_once_and_separate_from_payroll(client):
    assert client.put("/api/payroll-settings", json={
        "payroll_enabled": True, "salary_gross": 100000,
    }).status_code == 200
    assert client.post("/api/vacations", json={
        "start_date": "2026-09-07", "end_date": "2026-09-13",
        "amount": 11000, "payment_date": "2026-09-04",
    }).status_code == 200
    payroll_before = client.get("/api/payroll-settings").json()
    future_before = cashflow_snapshot(1)["timeline"]
    assert cashflow_snapshot(1)["current_cash"] == 28000

    for _ in range(2):
        response = client.put("/api/buffer/vacation-reserve", json={"amount": 11000})
        assert response.status_code == 200
        assert response.json()["start_capital"] == 28000
        flow = cashflow_snapshot(1)
        assert flow["current_cash"] == 39000
        assert flow["piggy_bank_balance"] == 0
        assert flow["buffer_balance"] > 11000
        assert [(r["date"], r["income"]) for r in flow["timeline"]] == [
            (r["date"], r["income"]) for r in future_before
        ]
    assert client.get("/api/payroll-settings").json() == payroll_before
    assert client.get("/api/buffer").json()["settings"]["initial_vacation_reserve"] == 11000
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert con.execute("SELECT initial_vacation_reserve FROM settings WHERE user_id=2").fetchone()[0] == 0

    # Saving the ordinary settings must not reset the separate reserve.
    client.put("/api/cashflow-settings", json={
        "cashflow_enabled": True, "start_date": "2026-09-15", "start_capital": 28000,
    })
    assert cashflow_snapshot(1)["current_cash"] == 39000
    assert client.post("/api/transactions", json={
        "type": "expense", "amount": 1000, "tx_date": "2026-09-16",
    }).status_code == 200
    assert cashflow_snapshot(1)["current_cash"] == 38000
    client.put("/api/buffer/vacation-reserve", json={"amount": 0})
    assert cashflow_snapshot(1)["current_cash"] == 27000


def test_invalid_reserve_does_not_change_saved_balance(client):
    client.put("/api/buffer/vacation-reserve", json={"amount": 11000})
    for value in (-1, "NaN", "Infinity"):
        assert client.put("/api/buffer/vacation-reserve", json={"amount": value}).status_code == 422
    assert cashflow_snapshot(1)["current_cash"] == 39000


def test_reserve_backup_and_old_backup_default(client):
    client.put("/api/buffer/vacation-reserve", json={"amount": 11000})
    backup = export_user_data(1)
    assert backup["data"]["settings"]["initial_vacation_reserve"] == 11000
    client.put("/api/buffer/vacation-reserve", json={"amount": 5})
    restore_user_data(1, backup)
    assert cashflow_snapshot(1)["current_cash"] == 39000
    del backup["data"]["settings"]["initial_vacation_reserve"]
    restore_user_data(1, backup)
    assert cashflow_snapshot(1)["current_cash"] == 28000


def test_existing_database_migrates_without_changing_start_capital(client):
    with connect() as con:
        con.execute("ALTER TABLE settings DROP COLUMN initial_vacation_reserve")
    init_db()
    init_db()
    assert cashflow_snapshot(1)["current_cash"] == 28000
    assert cashflow_snapshot(1)["settings"]["initial_vacation_reserve"] == 0
