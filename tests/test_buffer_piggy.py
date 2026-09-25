from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.cashflow_app import app, cashflow_snapshot, piggy_bank_balance
from app.db import connect, ensure_user, init_db

TODAY = date(2026, 9, 13)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Card with 1 000 ₽ for the ten days 13–22 September (100 ₽ a day)."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    init_db()
    ensure_user(1)
    with connect() as con:
        con.execute("UPDATE income_rules SET amount=100000 WHERE user_id=1")
        con.execute(
            "UPDATE settings SET cashflow_enabled=1,cashflow_start_date=?,"
            "cashflow_start_capital=1000,forecast_months=1 WHERE user_id=1",
            (TODAY.isoformat(),),
        )
    with TestClient(app) as value:
        yield value


def spend(client, value, day=TODAY):
    response = client.post("/api/transactions", json={"type": "expense", "amount": value, "tx_date": day.isoformat()})
    assert response.status_code == 200


def test_home_screen_shows_card_limit(client):
    flow = cashflow_snapshot(1)
    assert flow["period"]["days_left"] == 10
    assert flow["today_target"] == 100
    assert flow["card_balance"] == 1000


def test_overspend_offers_two_ways_and_never_uses_buffer(client):
    before = cashflow_snapshot(1)
    spend(client, 1100)
    flow = cashflow_snapshot(1)
    assert flow["available_today"] == -1000
    assert flow["overspend"] == 1000
    assert flow["buffer_balance"] == before["buffer_balance"]
    assert flow["tomorrow_limit"] == 0  # variant "spread over remaining days"

    # Variant "take from the piggy bank".
    assert client.post("/api/piggy-bank/deposit", json={"amount": 5000, "movement_date": "2026-09-13"}).status_code == 200
    too_much = client.post("/api/piggy-bank/to-card", json={"amount": 1000.01, "purpose": "cover_overspend"})
    assert too_much.status_code == 422
    cover = client.post("/api/piggy-bank/to-card", json={"amount": 1000, "purpose": "cover_overspend"})
    assert cover.status_code == 200
    assert cover.json()["balance"] == 4000
    flow = cashflow_snapshot(1)
    assert flow["available_today"] == 0
    assert flow["overspend"] == 0
    assert flow["tomorrow_limit"] == 100
    assert flow["buffer_balance"] == before["buffer_balance"]


def test_piggy_to_card_for_today_adds_to_todays_sum(client):
    assert client.post("/api/piggy-bank/deposit", json={"amount": 5000, "movement_date": "2026-09-13"}).status_code == 200
    # The transfer may come before the purchase and is not limited by an overspend.
    response = client.post("/api/piggy-bank/to-card", json={"amount": 1400, "purpose": "today"})
    assert response.status_code == 200
    assert response.json()["balance"] == 3600
    flow = cashflow_snapshot(1)
    assert flow["today_target"] == 1500
    assert flow["available_today"] == 1500
    spend(client, 1400)
    spend(client, 100)
    flow = cashflow_snapshot(1)
    assert flow["today_target"] == 1500
    assert flow["available_today"] == 0
    assert flow["overspend"] == 0
    assert flow["tomorrow_limit"] == 100  # the other days are not touched
    assert flow["card_balance"] == 900


def test_piggy_to_card_cannot_exceed_piggy_balance(client):
    assert client.post("/api/piggy-bank/deposit", json={"amount": 300, "movement_date": "2026-09-13"}).status_code == 200
    assert client.post("/api/piggy-bank/to-card", json={"amount": 300.01}).status_code == 422
    assert piggy_bank_balance(1) == 300
    response = client.post("/api/piggy-bank/to-card", json={"amount": 300})
    assert response.status_code == 200
    assert response.json()["balance"] == 0
    flow = cashflow_snapshot(1)
    assert flow["today_target"] == 130  # 1 300 ₽ over ten days
    assert flow["card_balance"] == 1300


def test_piggy_changes_only_by_explicit_operations(client):
    assert client.post("/api/piggy-bank/deposit", json={"amount": 500, "movement_date": "2026-09-13"}).status_code == 200
    spend(client, 2000)
    assert client.post("/api/transactions", json={"type": "income", "amount": 700, "tx_date": "2026-09-13"}).status_code == 200
    assert client.post("/api/transactions", json={
        "type": "income", "amount": 700, "tx_date": "2026-09-13", "income_destination": "buffer",
    }).status_code == 200
    bill = client.post("/api/bill-rules", json={"title": "Связь", "amount": 400, "day_of_month": 20}).json()
    assert client.post(f"/api/bills/{bill['id']}/pay", json={"due_date": "2026-09-20"}).status_code == 200
    cashflow_snapshot(1)
    assert piggy_bank_balance(1) == 500


def test_daily_remainder_moves_to_piggy_only_up_to_available(client):
    spend(client, 40)
    assert client.post("/api/piggy-bank/deposit", json={
        "amount": 60.01, "movement_date": "2026-09-13", "source": "daily_budget",
    }).status_code == 422
    assert client.post("/api/piggy-bank/deposit", json={
        "amount": 60, "movement_date": "2026-09-13", "source": "daily_budget",
    }).status_code == 200
    flow = cashflow_snapshot(1)
    assert flow["available_today"] == 0
    assert flow["tomorrow_limit"] == 100
    assert flow["piggy_bank_balance"] == 60
    # External piggy operations never touch the card.
    assert client.post("/api/piggy-bank/withdraw", json={"amount": 10, "movement_date": "2026-09-13"}).status_code == 200
    assert cashflow_snapshot(1)["card_balance"] == flow["card_balance"]


def test_buffer_rows_follow_actual_paydays(client):
    rows = cashflow_snapshot(1)["periods"]
    assert (rows[0]["start"], rows[0]["end"], rows[0]["kind"]) == ("2026-09-13", "2026-09-21", "Старт")
    assert (rows[1]["start"], rows[1]["end"], rows[1]["payday"]) == ("2026-09-22", "2026-10-06", "2026-09-22")
    assert rows[1]["card_period"]["start"] == "2026-09-23"
    assert rows[1]["received"] == 100000
    assert rows[1]["to_card"] > 0
    for row in rows:
        assert row["buffer"] == round(row["buffer_start"] + row["received"] - row["mandatory"] - row["to_card"], 2)
