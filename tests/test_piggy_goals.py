"""Цели копилки: «Отпуск» на 50 000 ₽ и сколько на него уже накоплено."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.backup import export_user_data, restore_user_data
from app.cashflow_app import app, cashflow_snapshot
from app.db import connect, ensure_user, init_db

TODAY = date(2026, 9, 13)


@pytest.fixture
def client(tmp_path, monkeypatch):
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


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def goal(client, title="Отпуск", target=50000):
    piggy = ok(client.post("/api/piggy-goals", json={"title": title, "target": target}))
    return next(g for g in piggy["goals"] if g["title"] == title)


def deposit(client, value, goal_id=None):
    body = {"amount": value, "movement_date": TODAY.isoformat()}
    if goal_id is not None:
        body["goal_id"] = goal_id
    return ok(client.post("/api/piggy-bank/deposit", json=body))


def by_id(piggy, goal_id):
    return next(g for g in piggy["goals"] if g["id"] == goal_id)


def test_deposit_for_a_goal_shows_the_percent(client):
    vacation = goal(client)
    assert vacation == {"id": vacation["id"], "title": "Отпуск", "target": 50000, "saved": 0, "left": 50000,
                        "percent": 0, "reached": False}
    piggy = deposit(client, 12500, vacation["id"])
    assert piggy["balance"] == 12500
    assert by_id(piggy, vacation["id"])["saved"] == 12500
    assert by_id(piggy, vacation["id"])["percent"] == 25
    assert by_id(piggy, vacation["id"])["left"] == 37500
    assert piggy["unassigned"] == 0
    # The history names the goal.
    assert piggy["movements"][0]["goal_title"] == "Отпуск"


def test_percent_is_rounded_down_and_reached_at_the_target(client):
    vacation = goal(client, target=1000)
    piggy = deposit(client, 999.99, vacation["id"])
    assert by_id(piggy, vacation["id"])["percent"] == 99
    assert not by_id(piggy, vacation["id"])["reached"]
    piggy = deposit(client, 100.01, vacation["id"])
    assert by_id(piggy, vacation["id"])["percent"] == 110
    assert by_id(piggy, vacation["id"])["reached"]
    assert by_id(piggy, vacation["id"])["left"] == 0


def test_goals_never_change_the_budget(client):
    before = cashflow_snapshot(1)
    vacation = goal(client)
    deposit(client, 3000)
    ok(client.post("/api/piggy-goals/transfer", json={"amount": 2000, "to_goal_id": vacation["id"]}))
    after = cashflow_snapshot(1)
    for key in ("available_today", "today_target", "buffer_balance", "card_balance"):
        assert after[key] == before[key]
    assert after["piggy_bank_balance"] == 3000


def test_put_aside_unassigned_money_and_take_it_back(client):
    vacation = goal(client)
    deposit(client, 3000)
    piggy = ok(client.post("/api/piggy-goals/transfer", json={"amount": 2000, "to_goal_id": vacation["id"]}))
    assert piggy["balance"] == 3000
    assert by_id(piggy, vacation["id"])["saved"] == 2000
    assert piggy["unassigned"] == 1000
    # Only the unassigned money can be set aside.
    too_much = client.post("/api/piggy-goals/transfer", json={"amount": 1000.01, "to_goal_id": vacation["id"]})
    assert too_much.status_code == 422
    assert "1 000 ₽" in too_much.json()["detail"]
    # «Убрать»: back to the unassigned money, at most what the goal has.
    assert client.post("/api/piggy-goals/transfer", json={"amount": 2000.01, "from_goal_id": vacation["id"]}).status_code == 422
    piggy = ok(client.post("/api/piggy-goals/transfer", json={"amount": 500, "from_goal_id": vacation["id"]}))
    assert by_id(piggy, vacation["id"])["saved"] == 1500
    assert piggy["unassigned"] == 1500
    assert piggy["balance"] == 3000


def test_transfer_between_two_goals(client):
    vacation = goal(client)
    phone = goal(client, "Телефон", 30000)
    deposit(client, 5000, vacation["id"])
    piggy = ok(client.post("/api/piggy-goals/transfer",
                           json={"amount": 1500, "from_goal_id": vacation["id"], "to_goal_id": phone["id"]}))
    assert by_id(piggy, vacation["id"])["saved"] == 3500
    assert by_id(piggy, phone["id"])["saved"] == 1500
    assert piggy["balance"] == 5000 and piggy["unassigned"] == 0
    # The same place twice, or a goal that does not exist.
    assert client.post("/api/piggy-goals/transfer", json={
        "amount": 1, "from_goal_id": phone["id"], "to_goal_id": phone["id"]}).status_code == 422
    assert client.post("/api/piggy-goals/transfer", json={"amount": 1}).status_code == 422
    assert client.post("/api/piggy-goals/transfer", json={
        "amount": 1, "from_goal_id": phone["id"], "to_goal_id": 999}).status_code == 404


def test_withdrawal_takes_goal_money_only_from_its_goal(client):
    vacation = goal(client)
    phone = goal(client, "Телефон", 30000)
    deposit(client, 5000, vacation["id"])
    deposit(client, 1000)
    # Without a goal only the unassigned 1 000 ₽ can leave.
    refused = client.post("/api/piggy-bank/withdraw", json={"amount": 1500, "movement_date": TODAY.isoformat()})
    assert refused.status_code == 422
    assert "Выберите цель" in refused.json()["detail"]
    ok(client.post("/api/piggy-bank/withdraw", json={"amount": 1000, "movement_date": TODAY.isoformat()}))
    # A goal gives at most what is set aside for it.
    assert client.post("/api/piggy-bank/withdraw", json={
        "amount": 1, "movement_date": TODAY.isoformat(), "goal_id": phone["id"]}).status_code == 422
    piggy = ok(client.post("/api/piggy-bank/withdraw", json={
        "amount": 4000, "movement_date": TODAY.isoformat(), "goal_id": vacation["id"], "note": "Билеты"}))
    assert piggy["balance"] == 1000
    assert by_id(piggy, vacation["id"])["saved"] == 1000
    assert piggy["unassigned"] == 0


def test_transfer_to_the_card_from_a_goal(client):
    vacation = goal(client)
    deposit(client, 5000, vacation["id"])
    assert client.post("/api/piggy-bank/to-card", json={"amount": 1000, "purpose": "today"}).status_code == 422
    piggy = ok(client.post("/api/piggy-bank/to-card",
                           json={"amount": 1000, "purpose": "today", "goal_id": vacation["id"]}))
    assert by_id(piggy, vacation["id"])["saved"] == 4000
    assert cashflow_snapshot(1)["today_target"] == 1100


def test_without_goals_the_piggy_bank_works_as_before(client):
    deposit(client, 2000)
    piggy = ok(client.post("/api/piggy-bank/withdraw", json={"amount": 2000, "movement_date": TODAY.isoformat()}))
    assert piggy["balance"] == 0
    assert piggy["goals"] == [] and piggy["unassigned"] == 0 and piggy["goals_shortfall"] == 0


def test_deleting_a_goal_deposit_takes_its_goal_money(client):
    vacation = goal(client)
    piggy = deposit(client, 5000, vacation["id"])
    movement = piggy["movements"][0]["id"]
    piggy = ok(client.delete(f"/api/piggy-bank/movements/{movement}"))
    assert piggy["balance"] == 0
    assert by_id(piggy, vacation["id"])["saved"] == 0


def test_deleting_money_already_set_aside_is_refused(client):
    vacation = goal(client)
    movement = deposit(client, 5000)["movements"][0]["id"]
    ok(client.post("/api/piggy-goals/transfer", json={"amount": 5000, "to_goal_id": vacation["id"]}))
    refused = client.delete(f"/api/piggy-bank/movements/{movement}")
    assert refused.status_code == 422
    assert "уберите деньги с цели" in refused.json()["detail"]
    ok(client.post("/api/piggy-goals/transfer", json={"amount": 5000, "from_goal_id": vacation["id"]}))
    assert ok(client.delete(f"/api/piggy-bank/movements/{movement}"))["balance"] == 0


def test_spending_from_the_piggy_bank_elsewhere_shows_a_shortfall(client):
    vacation = goal(client)
    deposit(client, 5000, vacation["id"])
    # An outside change (e.g. an expense paid from the piggy bank) is not
    # blocked by goals: the screen shows the gap instead.
    with connect() as con:
        con.execute("INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note) "
                    "VALUES(1,'withdraw',2000,?,'')", (TODAY.isoformat(),))
    piggy = ok(client.get("/api/piggy-bank"))
    assert piggy["balance"] == 3000
    assert piggy["goals_shortfall"] == 2000
    assert piggy["unassigned"] == 0


def test_edit_and_delete_a_goal(client):
    vacation = goal(client)
    deposit(client, 5000, vacation["id"])
    piggy = ok(client.put(f"/api/piggy-goals/{vacation['id']}", json={"title": "Отпуск в Турции", "target": 10000}))
    assert by_id(piggy, vacation["id"])["title"] == "Отпуск в Турции"
    assert by_id(piggy, vacation["id"])["percent"] == 50
    # The money of a deleted goal stays in the piggy bank, for no goal.
    piggy = ok(client.delete(f"/api/piggy-goals/{vacation['id']}"))
    assert piggy["goals"] == []
    assert piggy["balance"] == 5000
    assert piggy["unassigned"] == 5000
    assert piggy["movements"][0]["goal_title"] is None
    assert client.delete(f"/api/piggy-goals/{vacation['id']}").status_code == 404


@pytest.mark.parametrize("body", [
    {"title": "", "target": 100}, {"title": "   ", "target": 100}, {"title": "Отпуск", "target": 0},
    {"title": "x" * 41, "target": 100},
])
def test_goal_needs_a_title_and_a_target(client, body):
    assert client.post("/api/piggy-goals", json=body).status_code == 422


def test_unknown_goal_is_not_found(client):
    response = client.post("/api/piggy-bank/deposit", json={"amount": 100, "movement_date": TODAY.isoformat(),
                                                           "goal_id": 999})
    assert response.status_code == 404
    # Nothing was deposited.
    assert ok(client.get("/api/piggy-bank"))["balance"] == 0


def test_goals_survive_a_backup(client):
    vacation = goal(client)
    goal(client, "Телефон", 30000)
    deposit(client, 5000, vacation["id"])
    deposit(client, 1000)
    ok(client.post("/api/piggy-goals/transfer", json={"amount": 600, "to_goal_id": vacation["id"]}))
    before = ok(client.get("/api/piggy-goals"))
    backup = export_user_data(1)
    assert backup["backup_version"] == 13
    restore_user_data(1, backup)
    after = ok(client.get("/api/piggy-goals"))
    assert [(g["title"], g["target"], g["saved"]) for g in after["goals"]] == \
        [(g["title"], g["target"], g["saved"]) for g in before["goals"]] == \
        [("Отпуск", 50000, 5600), ("Телефон", 30000, 0)]
    assert after["unassigned"] == 400
    # The deposit for the goal still takes its goal money when deleted.
    piggy = ok(client.get("/api/piggy-bank"))
    linked = next(m for m in piggy["movements"] if m["goal_title"] == "Отпуск")
    piggy = ok(client.delete(f"/api/piggy-bank/movements/{linked['id']}"))
    assert by_id(piggy, after["goals"][0]["id"])["saved"] == 600


def test_backup_before_goals_restores_without_them(client):
    deposit(client, 1000)
    backup = export_user_data(1)
    backup["backup_version"] = 12
    for table in ("piggy_goals", "piggy_goal_moves"):
        backup["data"].pop(table)
    restore_user_data(1, backup)
    assert ok(client.get("/api/piggy-goals"))["goals"] == []


def test_backup_with_a_broken_goal_link_is_refused(client):
    vacation = goal(client)
    deposit(client, 1000, vacation["id"])
    backup = export_user_data(1)
    backup["data"]["piggy_goal_moves"][0]["goal_id"] = 999
    with pytest.raises(ValueError):
        restore_user_data(1, backup)
