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


# ---------- Stage 2: transfers, corrections, history, payroll income ----------


def balances(client):
    snap = ok(client.get("/api/accounts"))
    return {a["title"]: a["balance"] for a in snap["accounts"]}, snap


def test_transfer_moves_money_between_two_accounts_only(client):
    snap = setup(client)
    c1, c2 = by_title(snap, "Карта 1")["id"], by_title(snap, "Карта 2")["id"]
    budget = ok(client.get("/api/cashflow"))["available_today"]
    ok(client.post("/api/accounts/transfers", json={"from_account_id": c1, "to_account_id": c2, "amount": 10000}))
    values, snap = balances(client)
    assert (values["Карта 1"], values["Карта 2"], snap["total"]) == (30000, 25000, 155000)
    assert ok(client.get("/api/cashflow"))["available_today"] == budget
    # Cash withdrawal is a transfer to a cash account.
    cash = by_title(ok(client.post("/api/accounts", json={"title": "Наличные", "kind": "cash"})), "Наличные")["id"]
    ok(client.post("/api/accounts/transfers", json={"from_account_id": c1, "to_account_id": cash, "amount": 3000}))
    values, snap = balances(client)
    assert (values["Карта 1"], values["Наличные"], snap["total"]) == (27000, 3000, 155000)


@pytest.mark.parametrize("change, status", [
    ({"to_account_id": "same"}, 422),
    ({"to_account_id": "credit"}, 422),
    ({"from_account_id": "credit"}, 422),
    ({"date": "2026-09-20"}, 422),
    ({"to_account_id": 999}, 404),
])
def test_transfer_validation(client, change, status):
    snap = setup(client)
    ids = {"same": by_title(snap, "Карта 1")["id"], "credit": by_title(snap, "Кредитка")["id"]}
    payload = {"from_account_id": ids["same"], "to_account_id": by_title(snap, "Карта 2")["id"], "amount": 10}
    payload.update({k: ids.get(v, v) for k, v in change.items()})
    assert client.post("/api/accounts/transfers", json=payload).status_code == status


def test_transfer_edit_and_delete_recalculate(client):
    snap = setup(client)
    c1, c2 = by_title(snap, "Карта 1")["id"], by_title(snap, "Карта 2")["id"]
    ok(client.post("/api/accounts/transfers", json={"from_account_id": c1, "to_account_id": c2, "amount": 100}))
    history = ok(client.get(f"/api/accounts/{c1}/history"))
    item = history["items"][0]
    assert item["type"] == "transfer" and item["amount"] == -100 and "Карта 2" in item["title"]
    ok(client.put(f"/api/accounts/transfers/{item['id']}",
                  json={"from_account_id": c1, "to_account_id": c2, "amount": 250}))
    assert balances(client)[0]["Карта 1"] == 39750
    ok(client.delete(f"/api/accounts/transfers/{item['id']}"))
    assert balances(client)[0]["Карта 1"] == 40000


def test_correct_balance_writes_the_difference_and_keeps_the_budget(client):
    snap = setup(client)
    c2 = by_title(snap, "Карта 2")["id"]
    budget = ok(client.get("/api/cashflow"))["available_today"]
    ok(client.post(f"/api/accounts/{c2}/adjust", json={"target": 15500, "note": "проценты"}))
    values, snap = balances(client)
    assert values["Карта 2"] == 15500 and snap["total"] == 155500
    assert ok(client.get("/api/cashflow"))["available_today"] == budget
    item = ok(client.get(f"/api/accounts/{c2}/history"))["items"][0]
    assert (item["type"], item["amount"], item["title"]) == ("adjustment", 500, "Исправление остатка")
    assert client.post(f"/api/accounts/{c2}/adjust", json={"target": 15500}).status_code == 422
    assert client.post(f"/api/accounts/{c2}/adjust", json={"target": 1, "date": "2026-09-01"}).status_code == 422
    ok(client.delete(f"/api/accounts/adjustments/{item['id']}"))
    assert balances(client)[0]["Карта 2"] == 15000


def test_correct_credit_debt_is_interest_or_cashback(client):
    snap = setup(client)
    credit = by_title(snap, "Кредитка")["id"]
    ok(client.post(f"/api/accounts/{credit}/adjust", json={"target": 700}))
    item = ok(client.get(f"/api/accounts/{credit}/history"))["items"][0]
    assert (item["reason"], item["amount"], item["title"]) == ("interest", -700, "Проценты банка")
    state = by_title(ok(client.get("/api/accounts")), "Кредитка")
    assert (state["debt"], state["available"]) == (700, 49300)
    ok(client.post(f"/api/accounts/{credit}/adjust", json={"target": 200}))
    item = ok(client.get(f"/api/accounts/{credit}/history"))["items"][0]
    assert (item["reason"], item["amount"]) == ("cashback", 500)
    assert ok(client.get("/api/accounts"))["total"] == 155000


def test_history_lists_operations_before_opening_as_not_counted(client):
    snap = setup(client)
    c1 = by_title(snap, "Карта 1")["id"]
    ok(client.post("/api/transactions", json={"type": "expense", "amount": 50, "tx_date": "2026-09-01", "account_id": c1}))
    ok(client.post("/api/transactions", json={"type": "income", "amount": 70, "account_id": c1}))
    items = ok(client.get(f"/api/accounts/{c1}/history"))["items"]
    assert [(i["type"], i["amount"], i["counted"]) for i in items] == [("income", 70, True), ("expense", -50, False)]


def test_income_arrives_on_the_chosen_card(client):
    snap = setup(client)
    c2 = by_title(snap, "Карта 2")["id"]
    ok(client.post("/api/transactions", json={"type": "income", "amount": 5000, "income_destination": "daily",
                                              "account_id": c2}))
    assert balances(client)[0]["Карта 2"] == 20000


def advance_key(client):
    rows = ok(client.get("/api/cashflow"))["periods"]
    return next(r for r in rows if r["payday"] == "2026-09-22")["override_key"]


def test_confirmed_payout_arrives_on_the_main_account(client, monkeypatch):
    setup(client)
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 22))
    key = advance_key(client)
    # Not confirmed: the balances stay as they were.
    assert balances(client)[0]["Карта 1"] == 40000
    ok(client.put(f"/api/cashflow/income-overrides/{key}", json={"amount": 60000, "confirmed": True}))
    values, snap = balances(client)
    assert values["Карта 1"] == 100000 and snap["total"] == 215000
    # A later correction of the amount follows; the reset removes the income.
    ok(client.put(f"/api/cashflow/income-overrides/{key}", json={"amount": 58000}))
    assert balances(client)[0]["Карта 1"] == 98000
    ok(client.delete(f"/api/cashflow/income-overrides/{key}"))
    assert balances(client)[0]["Карта 1"] == 40000


def test_confirmed_payout_can_go_to_another_card(client, monkeypatch):
    snap = setup(client)
    c2 = by_title(snap, "Карта 2")["id"]
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 22))
    ok(client.put(f"/api/cashflow/income-overrides/{advance_key(client)}",
                  json={"amount": 1000, "confirmed": True, "account_id": c2}))
    values, _ = balances(client)
    assert (values["Карта 1"], values["Карта 2"]) == (40000, 16000)


def test_payout_counted_without_confirmation_offers_to_record_the_income(client, monkeypatch):
    setup(client)
    monkeypatch.setattr(clock, "today", lambda: date(2026, 10, 7))
    snap = ok(client.get("/api/accounts"))
    hints = [h for h in snap["hints"] if h["type"] == "payroll_income"]
    # 22.09 is counted by the budget on its own; 07.10 still waits for «Получил ✓».
    assert [h["payday"] for h in hints] == ["2026-09-22"]
    assert hints[0]["confirmed"] is False and hints[0]["amount"] > 0
    snap = ok(client.post("/api/accounts/payroll-income", json={"period_start": hints[0]["period_start"]}))
    assert by_title(snap, "Карта 1")["balance"] == 40000 + hints[0]["amount"]
    assert snap["hints"] == []
    assert client.post("/api/accounts/payroll-income", json={"period_start": hints[0]["period_start"]}).status_code == 422


def test_irregular_income_keeps_its_card(client):
    snap = setup(client)
    c2 = by_title(snap, "Карта 2")["id"]
    ok(client.put("/api/budget-mode", json={"budget_mode": "irregular", "start_date": TODAY.isoformat(),
                                             "start_total": 10000, "start_reserve": 0}))
    income = ok(client.post("/api/irregular/incomes", json={"amount": 1000, "account_id": c2}))
    assert balances(client)[0]["Карта 2"] == 16000
    ok(client.put(f"/api/irregular/incomes/{income['id']}", json={"amount": 2000}))
    assert balances(client)[0]["Карта 2"] == 17000
    listed = ok(client.get("/api/irregular/incomes"))[0]
    assert listed["account_id"] == c2
