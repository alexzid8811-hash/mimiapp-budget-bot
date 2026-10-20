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


# «Максимум в день».

def test_daily_cap_keeps_excess_in_buffer(client):
    before = cashflow_snapshot(1)
    response = client.put("/api/daily-cap", json={"amount": 80})
    assert response.status_code == 200
    assert response.json()["daily_cap"] == 80
    flow = cashflow_snapshot(1)
    assert flow["daily_cap"] == 80
    assert flow["today_target"] == 80
    assert flow["card_balance"] == 800
    assert flow["buffer_balance"] == before["buffer_balance"] + 200
    assert flow["period_cap_kept"] == 200
    # Later periods are capped too.
    assert flow["next_period"]["daily"] == 80
    assert all(row["daily"] <= 80 for row in flow["periods"])
    for row in flow["periods"]:
        assert row["buffer"] == round(
            row["buffer_start"] + row["received"] - row["mandatory"] - row["to_card"] - row["to_piggy"], 2)


def test_daily_cap_above_the_limit_does_nothing(client):
    before = cashflow_snapshot(1)
    assert client.put("/api/daily-cap", json={"amount": 150}).status_code == 200
    flow = cashflow_snapshot(1)
    assert flow["today_target"] == before["today_target"] == 100
    assert flow["buffer_balance"] == before["buffer_balance"]
    assert flow["period_cap_kept"] == 0


def test_daily_cap_changed_mid_period(client, monkeypatch):
    spend(client, 100)
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 14))
    before = cashflow_snapshot(1)
    assert before["today_target"] == 100  # 900 ₽ for nine days
    assert client.put("/api/daily-cap", json={"amount": 80}).status_code == 200
    flow = cashflow_snapshot(1)
    assert flow["today_target"] == 80
    assert flow["card_balance"] == 720
    assert flow["buffer_balance"] == before["buffer_balance"] + 180
    # Raised again the next day: the card gets the difference back from the
    # buffer (only as much as the buffer can safely give).
    spend(client, 80, date(2026, 9, 14))
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 15))
    assert cashflow_snapshot(1)["today_target"] == 80
    assert client.put("/api/daily-cap", json={"amount": None}).status_code == 200
    flow = cashflow_snapshot(1)
    assert flow["daily_cap"] is None
    # 640 ₽ left on the card + the 180 ₽ returned to the buffer on the 14th.
    assert flow["card_balance"] == 820
    assert flow["today_target"] == 102.5
    assert flow["capital_shortfall"] == 0
    # History of the days gone is not rewritten.
    assert client.get("/api/cashflow-settings").json()["daily_cap"] is None


def test_spare_buffer_moves_to_piggy_without_touching_the_plan(client):
    assert client.put("/api/daily-cap", json={"amount": 80}).status_code == 200
    flow = cashflow_snapshot(1)
    spare = flow["spare_buffer"]
    assert spare > 0
    too_much = client.post("/api/buffer/to-piggy", json={"amount": spare + 1})
    assert too_much.status_code == 422
    response = client.post("/api/buffer/to-piggy", json={"amount": spare})
    assert response.status_code == 200
    assert response.json()["balance"] == spare
    after = cashflow_snapshot(1)
    assert after["buffer_balance"] == round(flow["buffer_balance"] - spare, 2)
    assert after["piggy_bank_balance"] == spare
    assert after["capital_shortfall"] == 0
    assert after["spare_buffer"] == 0
    assert [r["to_card"] for r in after["periods"]] == [r["to_card"] for r in flow["periods"]]
    assert after["periods"][0]["to_piggy"] == spare


def test_no_spare_buffer_without_daily_cap(client):
    assert cashflow_snapshot(1)["spare_buffer"] == 0
    assert client.post("/api/buffer/to-piggy", json={"amount": 1}).status_code == 422


def test_daily_cap_survives_backup(client):
    assert client.put("/api/daily-cap", json={"amount": 80}).status_code == 200
    spare = cashflow_snapshot(1)["spare_buffer"]
    assert client.post("/api/buffer/to-piggy", json={"amount": spare}).status_code == 200
    before = cashflow_snapshot(1)
    backup = client.get("/api/backup").json()
    assert backup["data"]["daily_caps"][0]["amount"] == 80
    assert client.put("/api/daily-cap", json={"amount": None}).status_code == 200
    assert client.post("/api/backup/restore", json=backup).status_code == 200
    after = cashflow_snapshot(1)
    for key in ("daily_cap", "today_target", "buffer_balance", "piggy_bank_balance", "period_cap_kept"):
        assert after[key] == before[key], key


def test_daily_cap_excess_to_piggy(client):
    before = cashflow_snapshot(1)
    response = client.put("/api/daily-cap", json={"amount": 80, "excess_to": "piggy"})
    assert response.status_code == 200
    assert response.json()["daily_cap_to"] == "piggy"
    flow = cashflow_snapshot(1)
    assert flow["today_target"] == 80
    assert flow["daily_cap_to"] == "piggy"
    # The buffer stays as without the maximum, the 200 ₽ went to the piggy bank.
    assert flow["buffer_balance"] == before["buffer_balance"]
    assert flow["piggy_bank_balance"] == 200
    assert flow["period_cap_kept"] == flow["period_cap_kept_piggy"] == 200
    assert flow["capital_shortfall"] == 0
    movements = client.get("/api/piggy-bank").json()["movements"]
    assert [(m["purpose"], m["amount"]) for m in movements] == [("cap_excess", 200)]
    # The deposit follows the budget and cannot be deleted by hand.
    assert client.delete(f"/api/piggy-bank/movements/{movements[0]['id']}").status_code == 422
    # Recomputing does not deposit twice.
    cashflow_snapshot(1)
    assert cashflow_snapshot(1)["piggy_bank_balance"] == 200
    for row in flow["periods"]:
        assert row["buffer"] == round(
            row["buffer_start"] + row["received"] - row["mandatory"] - row["to_card"] - row["to_piggy"], 2)


def test_switching_excess_back_to_buffer_keeps_what_went_to_piggy(client, monkeypatch):
    assert client.put("/api/daily-cap", json={"amount": 80, "excess_to": "piggy"}).status_code == 200
    assert cashflow_snapshot(1)["piggy_bank_balance"] == 200
    spend(client, 80)
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 14))
    assert client.put("/api/daily-cap", json={"amount": 50, "excess_to": "buffer"}).status_code == 200
    flow = cashflow_snapshot(1)
    assert flow["daily_cap_to"] == "buffer"
    assert flow["today_target"] == 50
    # 720 ₽ for nine days -> 450 ₽; the 270 ₽ go to the buffer now.
    assert flow["piggy_bank_balance"] == 200
    assert flow["card_balance"] == 450


def test_switching_excess_to_piggy_moves_the_current_periods_excess(client, monkeypatch):
    plain = cashflow_snapshot(1)
    assert client.put("/api/daily-cap", json={"amount": 80}).status_code == 200
    assert cashflow_snapshot(1)["buffer_balance"] == plain["buffer_balance"] + 200
    spend(client, 80)
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 14))
    assert client.put("/api/daily-cap", json={"amount": 80, "excess_to": "piggy"}).status_code == 200
    flow = cashflow_snapshot(1)
    # The 200 ₽ kept in the buffer on 13 September go to the piggy bank:
    # the buffer is again as without the maximum.
    assert flow["piggy_bank_balance"] == 200
    assert flow["buffer_balance"] == plain["buffer_balance"]
    assert flow["period_cap_kept"] == flow["period_cap_kept_piggy"] == 200
    assert flow["today_target"] == 80
    # Saving the same choice again the same day does not move it twice.
    assert client.put("/api/daily-cap", json={"amount": 80, "excess_to": "piggy"}).status_code == 200
    assert cashflow_snapshot(1)["piggy_bank_balance"] == 200
