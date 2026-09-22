from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.backup import export_user_data, restore_user_data
from app.budget import Period
from app.cashflow_app import app, cashflow_snapshot
from app.db import connect, ensure_user, init_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 15))
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    init_db()
    ensure_user(1)
    with connect() as con:
        con.execute(
            "UPDATE settings SET cashflow_enabled=1,cashflow_start_date='2026-09-15',"
            "cashflow_start_capital=1000,forecast_months=1 WHERE user_id=1"
        )
    with TestClient(app) as value:
        yield value


def confirm_physical_buffer(client, balance=600):
    response = client.put(
        "/api/buffer/account-balance",
        json={"balance": balance, "note": "Фактический остаток"},
    )
    assert response.status_code == 200
    return response.json()


def test_expense_does_not_change_confirmed_buffer(client):
    before_setup = client.get("/api/cashflow").json()
    assert before_setup["buffer_setup_required"] is True

    confirm_physical_buffer(client)
    before = client.get("/api/cashflow").json()
    assert (before["current_cash"], before["card_balance"], before["buffer_balance"]) == (1000, 400, 600)

    created = client.post(
        "/api/transactions",
        json={"type": "expense", "amount": 125, "tx_date": "2026-09-15", "note": "Кофе"},
    )
    assert created.status_code == 200

    after = client.get("/api/cashflow").json()
    assert after["current_cash"] == 875
    assert after["card_balance"] == 275
    assert after["buffer_balance"] == 600
    assert after["buffer_is_physical"] is True


def test_only_explicit_transfers_change_physical_buffer(client):
    confirm_physical_buffer(client)
    before = cashflow_snapshot(1)

    moved = client.post("/api/buffer/transfer-to", json={"amount": 100, "note": "Перевод"})
    assert moved.status_code == 200
    after_deposit = cashflow_snapshot(1)
    assert after_deposit["current_cash"] == before["current_cash"]
    assert after_deposit["buffer_balance"] == before["buffer_balance"] + 100
    assert after_deposit["card_balance"] == before["card_balance"] - 100

    too_much = client.post(
        "/api/buffer/transfer-to",
        json={"amount": max(0, after_deposit["available_cash"]) + 0.01},
    )
    assert too_much.status_code == 422

    returned = client.post("/api/buffer/transfer-from", json={"amount": 40})
    assert returned.status_code == 200
    after_withdraw = cashflow_snapshot(1)
    assert after_withdraw["current_cash"] == before["current_cash"]
    assert after_withdraw["buffer_balance"] == before["buffer_balance"] + 60
    assert after_withdraw["card_balance"] == before["card_balance"] - 60


def test_card_overdraft_carries_into_the_next_period_without_using_buffer(client, monkeypatch):
    """A new period must retain debt from the card and recalculate its limit."""
    first_day = date(2026, 9, 15)
    next_day = date(2026, 9, 16)

    def period_for(_user_id, as_of):
        if as_of <= first_day:
            return Period(first_day, first_day)
        return Period(next_day, date(2026, 9, 17))

    monkeypatch.setattr("app.cashflow_app.planning.user_period", period_for)
    # Period-table forecasting is unrelated to the card/buffer invariant.
    monkeypatch.setattr("app.cashflow_app.cashflow_period_rows", lambda *_args, **_kwargs: [])
    confirm_physical_buffer(client)

    expense = client.post(
        "/api/transactions",
        json={"type": "expense", "amount": 500, "tx_date": first_day.isoformat()},
    )
    assert expense.status_code == 200
    before_transition = cashflow_snapshot(1)
    assert before_transition["remaining_period"] < 0
    assert before_transition["buffer_balance"] == 600

    monkeypatch.setattr(clock, "today", lambda: next_day)
    after_transition = cashflow_snapshot(1)
    assert after_transition["card_balance"] == -100
    assert after_transition["remaining_period"] == -100
    assert after_transition["daily_target"] == -50
    assert after_transition["available_today"] == -50
    assert after_transition["buffer_balance"] == 600


def test_buffer_income_stays_out_of_card_and_round_trips_backup(client):
    confirm_physical_buffer(client)
    before = cashflow_snapshot(1)

    income = client.post(
        "/api/transactions",
        json={
            "type": "income",
            "amount": 300,
            "tx_date": "2026-09-15",
            "income_destination": "buffer",
            "note": "Премия",
        },
    )
    assert income.status_code == 200
    tx_id = income.json()["id"]
    after_income = cashflow_snapshot(1)
    assert after_income["current_cash"] == before["current_cash"] + 300
    assert after_income["buffer_balance"] == before["buffer_balance"] + 300
    assert after_income["card_balance"] == before["card_balance"]

    updated = client.put(
        f"/api/transactions/{tx_id}",
        json={
            "type": "income",
            "amount": 250,
            "tx_date": "2026-09-15",
            "income_destination": "buffer",
            "note": "Премия уточнена",
        },
    )
    assert updated.status_code == 200
    after_update = cashflow_snapshot(1)
    assert after_update["buffer_balance"] == before["buffer_balance"] + 250
    assert after_update["card_balance"] == before["card_balance"]

    backup = export_user_data(1)
    assert backup["backup_version"] == 8
    assert backup["data"]["settings"]["buffer_account_balance"] == after_update["buffer_balance"]
    assert backup["data"]["buffer_account_movements"][-1]["income_transaction_id"] == tx_id

    ensure_user(2)
    restore_user_data(2, backup)
    restored = cashflow_snapshot(2)
    assert restored["buffer_is_physical"] is True
    assert restored["buffer_balance"] == after_update["buffer_balance"]
    assert restored["card_balance"] == after_update["card_balance"]
