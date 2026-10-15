"""API of cards and accounts: switch, setup wizard, CRUD, order, archive."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.cashflow_app import app
from app.db import connect, ensure_user, init_db, user_scope

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


def setup(client, accounts=None, envelopes=None):
    accounts = accounts or [
        {"title": "Карта 1", "kind": "debit", "balance": 40000, "is_main": True},
        {"title": "Карта 2", "kind": "debit", "balance": 15000},
        {"title": "Накопительный", "kind": "savings", "balance": 100000},
        {"title": "Кредитка", "kind": "credit", "balance": 0, "credit_limit": 50000, "payment_day": 25},
    ]
    return ok(client.post("/api/accounts/setup", json={"accounts": accounts, "envelopes": envelopes or {}}))


def by_title(snapshot, title):
    return next(a for a in snapshot["accounts"] if a["title"] == title)


def test_feature_is_off_until_accounts_with_balances_are_added(client):
    snap = ok(client.get("/api/accounts"))
    assert snap["enabled"] is False and snap["accounts"] == []
    # The switch alone cannot turn it on: first the cards and their money.
    response = client.put("/api/accounts/settings", json={"enabled": True})
    assert response.status_code == 422
    assert "Сначала добавьте карты" in response.json()["detail"]


def test_wizard_prefill_reads_the_envelopes_without_enabling(client):
    snap = ok(client.get("/api/accounts", params={"with_amounts": True}))
    assert snap["enabled"] is False and snap["budget_configured"] is True
    amounts = {e["key"]: e["amount"] for e in snap["envelopes"]}
    assert set(amounts) == {"spending", "buffer", "piggy"}
    assert amounts["spending"] + amounts["buffer"] == 1000


def test_setup_creates_accounts_main_and_envelopes(client):
    snap = setup(client, envelopes={"piggy": 1})
    assert snap["enabled"] is True
    assert snap["total"] == 155000 and snap["credit_debt"] == 0
    main = by_title(snap, "Карта 1")
    assert main["is_main"] and snap["main_account_id"] == main["id"]
    assert main["opening_date"] == TODAY.isoformat()
    assert [a["title"] for a in snap["accounts"]] == ["Карта 1", "Карта 2", "Накопительный", "Кредитка"]
    credit = by_title(snap, "Кредитка")
    assert credit["debt"] == 0 and credit["available"] == 50000 and credit["payment_day"] == 25
    placed = {e["key"]: e["account_id"] for e in snap["envelopes"]}
    assert placed == {"spending": main["id"], "buffer": main["id"], "piggy": by_title(snap, "Карта 2")["id"]}
    assert main["envelopes"] == ["spending", "buffer"]
    # The wizard runs once.
    assert client.post("/api/accounts/setup", json={"accounts": [{"title": "X", "is_main": True}]}).status_code == 409


@pytest.mark.parametrize("payload, message", [
    ({"accounts": [{"title": "A"}]}, "основной"),
    ({"accounts": [{"title": "К", "kind": "credit", "is_main": True}]}, "Кредитка"),
    ({"accounts": [{"title": "A", "is_main": True}, {"title": "К", "kind": "credit"}],
      "envelopes": {"piggy": 1}}, "кредитку"),
    ({"accounts": [{"title": "A", "is_main": True}], "envelopes": {"reserve": 0}}, "режиме"),
    ({"accounts": [{"title": "К", "kind": "credit", "balance": -5, "is_main": False}]}, "отрицательным"),
])
def test_setup_validation(client, payload, message):
    response = client.post("/api/accounts/setup", json=payload)
    assert response.status_code == 422
    assert message in str(response.json()["detail"])
    assert ok(client.get("/api/accounts"))["accounts"] == []


def test_crud_main_and_credit_rules(client):
    snap = setup(client)
    card2 = by_title(snap, "Карта 2")
    credit = by_title(snap, "Кредитка")
    snap = ok(client.put(f"/api/accounts/{card2['id']}", json={"title": "Зарплатная", "emoji": "💼", "is_main": True}))
    assert by_title(snap, "Зарплатная")["is_main"] and snap["main_account_id"] == card2["id"]
    response = client.put(f"/api/accounts/{credit['id']}", json={"title": "Кредитка", "is_main": True})
    assert response.status_code == 422
    snap = ok(client.put(f"/api/accounts/{credit['id']}",
                         json={"title": "Кредитка", "credit_limit": 70000, "payment_day": 5}))
    assert by_title(snap, "Кредитка")["available"] == 70000
    snap = ok(client.post("/api/accounts", json={"title": "Кошелёк", "emoji": "👛", "kind": "cash", "balance": 500}))
    assert by_title(snap, "Кошелёк")["kind"] == "cash" and snap["total"] == 155500
    # A new account stands at the end of the active list.
    assert snap["accounts"][-1]["title"] == "Кошелёк"
    assert client.put("/api/accounts/999", json={"title": "x"}).status_code == 404


def test_order_follows_drag_and_drop(client):
    snap = setup(client)
    ids = [a["id"] for a in snap["accounts"]]
    ok(client.put("/api/accounts/order", json={"account_ids": list(reversed(ids))}))
    assert [a["id"] for a in ok(client.get("/api/accounts"))["accounts"]] == list(reversed(ids))
    assert client.put("/api/accounts/order", json={"account_ids": ids[:2]}).status_code == 422
    assert client.put("/api/accounts/order", json={"account_ids": [ids[0]] * 4}).status_code == 422


def test_archive_rules_and_restore(client):
    snap = setup(client, envelopes={"piggy": 1})
    main, card2, savings = (by_title(snap, t) for t in ("Карта 1", "Карта 2", "Накопительный"))
    # The main account and an account with envelopes stay active.
    response = client.post(f"/api/accounts/{main['id']}/archive")
    assert response.status_code == 422 and "основной" in response.json()["detail"]
    response = client.post(f"/api/accounts/{card2['id']}/archive")
    assert response.status_code == 422 and "копилка" in response.json()["detail"]
    # Money on the account: move it first.
    response = client.post(f"/api/accounts/{savings['id']}/archive")
    assert response.status_code == 422 and "остаток" in response.json()["detail"]

    empty = ok(client.post("/api/accounts", json={"title": "Пустая", "balance": 0}))
    empty_id = by_title(empty, "Пустая")["id"]
    snap = ok(client.post(f"/api/accounts/{empty_id}/archive"))
    archived = by_title(snap, "Пустая")
    assert archived["archived"] is True
    assert snap["accounts"][-1]["id"] == empty_id
    ok(client.put("/api/accounts/order", json={"account_ids": [a["id"] for a in snap["accounts"] if not a["archived"]]}))
    snap = ok(client.post(f"/api/accounts/{empty_id}/restore"))
    restored = by_title(snap, "Пустая")
    assert restored["archived"] is False
    assert [a["id"] for a in snap["accounts"] if not a["archived"]][-1] == empty_id


def test_delete_only_without_operations_otherwise_archive(client):
    snap = setup(client)
    card2 = by_title(snap, "Карта 2")
    ok(client.post("/api/transactions", json={"type": "expense", "amount": 15000, "account_id": card2["id"]}))
    snap = ok(client.delete(f"/api/accounts/{card2['id']}"))
    assert snap["outcome"] == "archived" and by_title(snap, "Карта 2")["archived"] is True
    # From the archive it cannot be deleted for good either.
    assert client.delete(f"/api/accounts/{card2['id']}").status_code == 422

    fresh = by_title(ok(client.post("/api/accounts", json={"title": "Новая", "balance": 100})), "Новая")
    snap = ok(client.delete(f"/api/accounts/{fresh['id']}"))
    assert snap["outcome"] == "deleted"
    assert all(a["title"] != "Новая" for a in snap["accounts"])


def test_expense_from_a_card_changes_its_balance_and_the_budget_as_before(client):
    snap = setup(client)
    card1 = by_title(snap, "Карта 1")
    before = ok(client.get("/api/cashflow"))["available_today"]
    tx = ok(client.post("/api/transactions", json={"type": "expense", "amount": 2000, "account_id": card1["id"]}))
    assert tx["account_id"] == card1["id"]
    snap = ok(client.get("/api/accounts"))
    assert by_title(snap, "Карта 1")["balance"] == 38000 and snap["total"] == 153000
    assert ok(client.get("/api/cashflow"))["available_today"] == before - 2000
    # An edit without the account (old app) keeps it; an explicit null removes it.
    ok(client.put(f"/api/transactions/{tx['id']}", json={"type": "expense", "amount": 1000}))
    assert by_title(ok(client.get("/api/accounts")), "Карта 1")["balance"] == 39000
    ok(client.put(f"/api/transactions/{tx['id']}", json={"type": "expense", "amount": 1000, "account_id": None}))
    assert by_title(ok(client.get("/api/accounts")), "Карта 1")["balance"] == 40000
    listed = ok(client.get("/api/transactions"))[0]
    assert listed["account_id"] is None


def test_operation_account_validation(client):
    snap = setup(client)
    credit = by_title(snap, "Кредитка")
    response = client.post("/api/transactions", json={"type": "income", "amount": 10, "account_id": credit["id"]})
    assert response.status_code == 422
    assert client.post("/api/transactions", json={"type": "expense", "amount": 10, "account_id": 999}).status_code == 422
    empty = by_title(ok(client.post("/api/accounts", json={"title": "Пустая"})), "Пустая")
    ok(client.post(f"/api/accounts/{empty['id']}/archive"))
    response = client.post("/api/transactions", json={"type": "expense", "amount": 10, "account_id": empty["id"]})
    assert response.status_code == 422 and "архиве" in response.json()["detail"]


def test_history_names_the_account_and_marks_the_archive(client):
    snap = setup(client)
    card2 = by_title(snap, "Карта 2")
    ok(client.post("/api/transactions", json={"type": "expense", "amount": 15000, "account_id": card2["id"]}))
    ok(client.post(f"/api/accounts/{card2['id']}/archive"))
    row = ok(client.get("/api/transactions"))[0]
    assert row["account_title"] == "Карта 2" and row["account_archived"] == 1


def test_operation_before_the_opening_date_does_not_touch_the_balance(client):
    snap = setup(client)
    card1 = by_title(snap, "Карта 1")
    ok(client.post("/api/transactions", json={"type": "expense", "amount": 500, "tx_date": "2026-09-10",
                                              "account_id": card1["id"]}))
    assert by_title(ok(client.get("/api/accounts")), "Карта 1")["balance"] == 40000


def test_switch_off_keeps_everything_and_counts_operations_on_return(client):
    setup(client)
    snap = ok(client.put("/api/accounts/settings", json={"enabled": False}))
    assert snap["enabled"] is False and len(snap["accounts"]) == 4
    with connect() as con:
        # created_at has a one-second resolution: make the operations "later".
        con.execute("UPDATE settings SET accounts_disabled_at='2000-01-01 00:00:00'")
    ok(client.post("/api/transactions", json={"type": "expense", "amount": 100}))
    ok(client.post("/api/piggy-bank/deposit", json={"amount": 50}))
    snap = ok(client.put("/api/accounts/settings", json={"enabled": True}))
    assert snap["enabled"] is True and snap["operations_without_account"] == 2
    assert ok(client.get("/api/accounts"))["operations_without_account"] == 0


def test_last_account_leaving_switches_the_feature_off(client):
    snap = setup(client, accounts=[{"title": "Единственная", "balance": 0, "is_main": True}])
    only = snap["accounts"][0]
    snap = ok(client.delete(f"/api/accounts/{only['id']}"))
    assert snap["outcome"] == "deleted" and snap["enabled"] is False and snap["main_account_id"] is None


def test_users_do_not_see_each_others_accounts(client):
    setup(client)
    ensure_user(2)
    with user_scope(2), connect() as con:
        assert con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    with user_scope(1), connect() as con:
        assert con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 4


def test_budget_and_old_responses_do_not_change_while_the_feature_is_off(client):
    before = {path: ok(client.get(path)) for path in ("/api/cashflow", "/api/dashboard")}
    ok(client.post("/api/transactions", json={"type": "expense", "amount": 300}))
    after_expense = ok(client.get("/api/cashflow"))
    assert after_expense["available_today"] == before["/api/cashflow"]["available_today"] - 300
    assert ok(client.get("/api/accounts"))["enabled"] is False
    tx = ok(client.get("/api/transactions"))[0]
    assert tx["account_id"] is None and tx["funding_envelope"] is None
