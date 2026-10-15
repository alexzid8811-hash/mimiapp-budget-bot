"""Reads a user's accounts and operations, runs :mod:`app.accounts` and builds
the API snapshot.  The budget is only read here, never changed."""
from __future__ import annotations

from datetime import date

from . import clock
from .accounts import Account, AccountsInput, Flow, Transfer, calculate
from .db import connect
from .money import amount, cents

ENVELOPES = {
    "payroll": ("spending", "buffer", "piggy"),
    "irregular": ("spending", "buffer", "piggy", "reserve"),
}
ENVELOPE_LABELS = {
    "payroll": {"spending": "на траты", "buffer": "буфер", "piggy": "копилка"},
    "irregular": {"spending": "свободные", "buffer": "на обязательные", "piggy": "копилка", "reserve": "резерв"},
}
KIND_TITLES = {"debit": "Карта", "savings": "Счёт", "credit": "Кредитка", "cash": "Наличные"}
ACCOUNT_COLUMNS = (
    "id,title,emoji,kind,opening_balance,opening_date,credit_limit,payment_day,sort_order,archived,created_at"
)


def settings_for(con, uid: int) -> dict:
    row = con.execute(
        "SELECT accounts_enabled,main_account_id,accounts_disabled_at,"
        "COALESCE(budget_mode,'payroll') AS budget_mode FROM settings WHERE user_id=?",
        (uid,),
    ).fetchone()
    data = dict(row) if row else {}
    return {
        "enabled": bool(data.get("accounts_enabled")),
        "main_account_id": data.get("main_account_id"),
        "disabled_at": data.get("accounts_disabled_at"),
        "mode": "irregular" if data.get("budget_mode") == "irregular" else "payroll",
    }


def is_enabled(uid: int, con=None) -> bool:
    if con is not None:
        return settings_for(con, uid)["enabled"]
    with connect() as own:
        return settings_for(own, uid)["enabled"]


def account_rows(con, uid: int) -> list[dict]:
    """Active accounts in the user's order, then archived ones."""
    return [dict(r) for r in con.execute(
        f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE user_id=? ORDER BY archived,sort_order,id", (uid,)
    )]


def _account(row: dict) -> Account:
    return Account(
        id=int(row["id"]), kind=row["kind"], opening_balance=cents(row["opening_balance"]),
        opening_date=date.fromisoformat(row["opening_date"]),
        credit_limit=cents(row["credit_limit"]) if row["credit_limit"] is not None else None,
        archived=bool(row["archived"]),
    )


def build_input(con, uid: int, rows: list[dict] | None = None) -> AccountsInput:
    rows = account_rows(con, uid) if rows is None else rows
    flows: list[Flow] = []
    for r in con.execute(
        "SELECT type,amount,tx_date,account_id FROM transactions WHERE user_id=? AND account_id IS NOT NULL",
        (uid,),
    ):
        value = cents(r["amount"])
        flows.append(Flow(int(r["account_id"]), date.fromisoformat(r["tx_date"]),
                          -value if r["type"] == "expense" else value))
    for r in con.execute("SELECT account_id,amount,date FROM account_incomes WHERE user_id=?", (uid,)):
        flows.append(Flow(int(r["account_id"]), date.fromisoformat(r["date"]), cents(r["amount"])))
    for r in con.execute("SELECT account_id,amount,date FROM account_adjustments WHERE user_id=?", (uid,)):
        flows.append(Flow(int(r["account_id"]), date.fromisoformat(r["date"]), cents(r["amount"])))
    transfers = [
        Transfer(int(r["from_account_id"]), r["to_account_id"], date.fromisoformat(r["date"]),
                 cents(r["amount"]), cents(r["interest_amount"] or 0))
        for r in con.execute(
            "SELECT from_account_id,to_account_id,amount,date,interest_amount "
            "FROM account_transfers WHERE user_id=?",
            (uid,),
        )
    ]
    return AccountsInput(accounts=[_account(r) for r in rows], flows=flows, transfers=transfers)


def envelope_accounts(con, uid: int) -> dict[str, int]:
    return {r["envelope"]: int(r["account_id"]) for r in con.execute(
        "SELECT envelope,account_id FROM envelope_accounts WHERE user_id=?", (uid,)
    )}


def envelope_amounts(uid: int, mode: str) -> dict[str, int] | None:
    """Today's actual amount of every envelope of the active mode, or None when
    the budget has no start (its numbers would be only illustrative)."""
    if mode == "irregular":
        from . import irregular_engine

        snap = irregular_engine.snapshot(uid)
        if not snap.get("configured"):
            return None
        return {
            "spending": cents(snap["free_balance"]),
            "buffer": cents(snap["bills_reserved"]),
            "piggy": cents(snap["piggy_bank_balance"]),
            "reserve": cents(snap["reserve_balance"]),
        }
    from . import engine

    if not engine.settings_for(uid)["cashflow_enabled"]:
        return None
    flow = engine.compute(uid)
    # Without an unconfirmed payday (``actual`` in engine.compute).  On a
    # payday the card money is already out of the buffer but reaches the card
    # only tomorrow: it is counted for spending.  Overdue unpaid bills are out
    # of the buffer in the plan, but the money is still there.
    return {
        "spending": cents(flow["card_balance"]) + cents(flow["pending_card_allocation"]),
        "buffer": cents(flow["buffer_balance"]) + cents(flow["reserved_mandatory"]),
        "piggy": cents(flow["piggy_bank_balance"]),
    }


def has_operations(con, uid: int, account_id: int) -> bool:
    checks = (
        ("SELECT 1 FROM transactions WHERE user_id=? AND account_id=? LIMIT 1", (uid, account_id)),
        ("SELECT 1 FROM account_transfers WHERE user_id=? AND (from_account_id=? OR to_account_id=?) LIMIT 1",
         (uid, account_id, account_id)),
        ("SELECT 1 FROM account_adjustments WHERE user_id=? AND account_id=? LIMIT 1", (uid, account_id)),
        ("SELECT 1 FROM account_incomes WHERE user_id=? AND account_id=? LIMIT 1", (uid, account_id)),
    )
    return any(con.execute(sql, params).fetchone() for sql, params in checks)


def operations_without_account(con, uid: int, since: str) -> int:
    """Operations recorded while the feature was off (``since`` is a UTC
    timestamp like ``created_at``)."""
    total = 0
    for sql in (
        "SELECT COUNT(*) FROM transactions WHERE user_id=? AND account_id IS NULL AND created_at>=?",
        "SELECT COUNT(*) FROM piggy_bank_movements WHERE user_id=? AND created_at>=? "
        "AND income_transaction_id IS NULL AND bill_payment_id IS NULL AND expense_transaction_id IS NULL",
        "SELECT COUNT(*) FROM emergency_reserve_movements WHERE user_id=? AND created_at>=? "
        "AND expense_transaction_id IS NULL",
    ):
        total += int(con.execute(sql, (uid, since)).fetchone()[0])
    return total


def snapshot(uid: int, *, with_amounts: bool = False) -> dict:
    """``with_amounts`` reads the envelopes even while the feature is off
    (the setup wizard fills the opening balances with them)."""
    with connect() as con:
        settings = settings_for(con, uid)
        rows = account_rows(con, uid)
        result = calculate(build_input(con, uid, rows))
        assigned = envelope_accounts(con, uid)
        operations = {int(r["id"]): has_operations(con, uid, int(r["id"])) for r in rows}
        pending = (operations_without_account(con, uid, settings["disabled_at"])
                   if settings["disabled_at"] else 0)
    mode = settings["mode"]
    main_id = settings["main_account_id"]
    keys = ENVELOPES[mode]
    amounts = envelope_amounts(uid, mode) if settings["enabled"] or with_amounts else None
    envelopes = [
        {
            "key": key,
            "label": ENVELOPE_LABELS[mode][key],
            "account_id": assigned.get(key, main_id),
            "amount": amount(amounts[key]) if amounts is not None else None,
        }
        for key in keys
    ]
    accounts = []
    for row in rows:
        state = result.states[int(row["id"])]
        accounts.append({
            **row,
            "kind_title": KIND_TITLES.get(row["kind"], row["kind"]),
            "archived": bool(row["archived"]),
            "is_main": int(row["id"]) == main_id,
            "balance": amount(state.balance),
            "debt": amount(state.debt) if state.kind == "credit" else None,
            "available": amount(state.available) if state.available is not None else None,
            "has_operations": operations[int(row["id"])],
            "envelopes": [e["key"] for e in envelopes if e["account_id"] == int(row["id"])],
        })
    return {
        "enabled": settings["enabled"],
        "today": clock.today().isoformat(),
        "mode": mode,
        "main_account_id": main_id,
        "budget_configured": amounts is not None,
        "total": amount(result.total),
        "credit_debt": amount(result.credit_debt),
        "accounts": accounts,
        "envelopes": envelopes,
        "operations_without_account": pending,
        "hints": payroll_income_hints(uid) if settings["enabled"] else [],
    }


# ---------- Operations of the accounts themselves ----------


def account_state(con, uid: int, account_id: int):
    return calculate(build_input(con, uid)).states[int(account_id)]


def add_adjustment(con, uid: int, account: dict, target: float, day: date, note: str) -> int:
    """Store the difference between the real amount and the calculated one.
    For a credit card ``target`` is the real debt: a growing debt is bank
    interest, a shrinking one is cashback or a refund."""
    state = account_state(con, uid, account["id"])
    credit = account["kind"] == "credit"
    wanted = -cents(target) if credit else cents(target)
    difference = wanted - state.balance
    if difference == 0:
        from fastapi import HTTPException

        raise HTTPException(422, "Сумма уже совпадает: исправлять нечего")
    reason = ("interest" if difference < 0 else "cashback") if credit else "correction"
    return con.execute(
        "INSERT INTO account_adjustments(user_id,account_id,amount,date,reason,note) VALUES(?,?,?,?,?,?)",
        (uid, account["id"], amount(difference), day.isoformat(), reason, note),
    ).lastrowid


def delete_adjustment(con, uid: int, row: dict) -> None:
    con.execute("DELETE FROM account_adjustments WHERE id=? AND user_id=?", (row["id"], uid))


def delete_transfer(con, uid: int, row: dict) -> None:
    con.execute("DELETE FROM account_transfers WHERE id=? AND user_id=?", (row["id"], uid))


ADJUSTMENT_TITLES = {"correction": "Исправление остатка", "interest": "Проценты банка", "cashback": "Кэшбэк, возврат"}
INCOME_TITLES = {"payroll": "Выплата", "piggy_external": "В копилку извне", "reserve_external": "В резерв извне"}
TRANSFER_TITLES = {"hint": "Перевод по подсказке", "reassign": "Перенос конверта", "credit_payment": "Погашение кредитки"}


def history(con, uid: int, account: dict) -> list[dict]:
    """Every operation of one account, newest first, with its signed effect on
    the money of the account (a credit card purchase is negative).  An
    operation dated before the opening date is listed but not counted."""
    account_id = int(account["id"])
    opening = account["opening_date"]
    names = {int(r["id"]): r for r in con.execute(
        "SELECT id,title,emoji,archived FROM accounts WHERE user_id=?", (uid,))}

    def name(other_id) -> str:
        other = names.get(int(other_id)) if other_id is not None else None
        if other is None:
            return "вне счетов"
        return f"{other['emoji']} {other['title']}" + (" (архив)" if other["archived"] else "")

    items: list[dict] = []
    for r in con.execute(
        "SELECT t.id,t.type,t.amount,t.tx_date,t.note,t.bill_rule_id,t.funding_envelope,"
        "c.title AS category_title,c.emoji AS category_emoji,b.title AS bill_title "
        "FROM transactions t LEFT JOIN categories c ON c.id=t.category_id AND c.user_id=t.user_id "
        "LEFT JOIN bill_rules b ON b.id=t.bill_rule_id AND b.user_id=t.user_id "
        "WHERE t.user_id=? AND t.account_id=?",
        (uid, account_id),
    ):
        expense = r["type"] == "expense"
        items.append({
            "type": "expense" if expense else "income", "id": r["id"], "date": r["tx_date"],
            "amount": -float(r["amount"]) if expense else float(r["amount"]),
            "title": r["bill_title"] or r["note"] or r["category_title"] or ("Расход" if expense else "Доход"),
            "sub": r["category_title"] or "", "emoji": r["category_emoji"] or ("💳" if expense else "💰"),
            "bill": r["bill_rule_id"] is not None,
        })
    for r in con.execute(
        "SELECT * FROM account_transfers WHERE user_id=? AND (from_account_id=? OR to_account_id=?)",
        (uid, account_id, account_id),
    ):
        outgoing = int(r["from_account_id"]) == account_id
        value = float(r["amount"])
        interest = float(r["interest_amount"] or 0)
        if outgoing:
            title = f"На {name(r['to_account_id'])}" if r["to_account_id"] is not None else "Ушло со счетов"
            signed = -value
        else:
            title = f"С {name(r['from_account_id'])}"
            signed = value - (interest if account["kind"] == "credit" else 0)
        sub = TRANSFER_TITLES.get(r["kind"], "Перевод")
        if r["kind"] == "linked":
            sub = "Копилка" if r["piggy_movement_id"] else "Резерв" if r["reserve_movement_id"] else "Платёж"
        if interest:
            sub += f" · из них проценты {interest:.2f}".replace(".", ",")
        items.append({
            "type": "transfer", "id": r["id"], "date": r["date"], "amount": signed, "title": title,
            "sub": " · ".join(x for x in (sub, r["note"]) if x), "emoji": "↔️", "kind": r["kind"],
            "deletable": r["kind"] in ("manual", "hint", "reassign"),
        })
    for r in con.execute("SELECT * FROM account_adjustments WHERE user_id=? AND account_id=?", (uid, account_id)):
        items.append({
            "type": "adjustment", "id": r["id"], "date": r["date"], "amount": float(r["amount"]),
            "title": ADJUSTMENT_TITLES.get(r["reason"], "Исправление"), "sub": r["note"] or "",
            "emoji": "✏️", "reason": r["reason"], "deletable": True,
        })
    for r in con.execute("SELECT * FROM account_incomes WHERE user_id=? AND account_id=?", (uid, account_id)):
        items.append({
            "type": "account_income", "id": r["id"], "date": r["date"], "amount": float(r["amount"]),
            "title": INCOME_TITLES.get(r["source"], "Приход"), "sub": r["note"] or "",
            "emoji": "💰", "source": r["source"],
        })
    for item in items:
        item["counted"] = item["date"] >= opening
    items.sort(key=lambda i: (i["date"], i["type"], i["id"]), reverse=True)
    return items


# ---------- Payroll income ----------


def accounts_since(con, uid: int) -> date | None:
    row = con.execute("SELECT MIN(opening_date) FROM accounts WHERE user_id=?", (uid,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def write_payroll_income(con, uid: int, period_start: str, payday: str, value: float, account_id: int) -> None:
    con.execute(
        "INSERT INTO account_incomes(user_id,account_id,amount,date,source,payroll_period_start) "
        "VALUES(?,?,?,?,'payroll',?) ON CONFLICT(user_id,payroll_period_start) "
        "WHERE payroll_period_start IS NOT NULL DO UPDATE SET amount=excluded.amount,"
        "account_id=excluded.account_id,date=excluded.date",
        (uid, account_id, value, payday, period_start),
    )


def sync_payroll_income(con, uid: int, *, period_start: str, payday: str, value: float, confirmed: bool,
                        account_id: int | None) -> None:
    """After «Получил ✓» (or an edit of a confirmed payout): the money arrives
    on the main account (or the chosen one).  An unconfirmed payout never
    touches a balance."""
    existing = con.execute(
        "SELECT account_id FROM account_incomes WHERE user_id=? AND payroll_period_start=?",
        (uid, period_start),
    ).fetchone()
    settings = settings_for(con, uid)
    if existing is None and not (confirmed and settings["enabled"]):
        return
    target = account_id or (existing["account_id"] if existing else settings["main_account_id"])
    if target is None or not value:
        return
    write_payroll_income(con, uid, period_start, payday, value, target)


def payroll_income_hints(uid: int) -> list[dict]:
    """Paydays the budget counts although no money was recorded on an
    account: confirmed while the feature was off, or counted by the budget
    on its own once the next payday came (it can no longer be confirmed)."""
    from datetime import timedelta

    from . import engine

    with connect() as con:
        settings = settings_for(con, uid)
        since = accounts_since(con, uid)
        recorded = {r[0] for r in con.execute(
            "SELECT payroll_period_start FROM account_incomes WHERE user_id=? AND payroll_period_start IS NOT NULL",
            (uid,))}
        main_id = settings["main_account_id"]
    budget = engine.settings_for(uid)
    if not settings["enabled"] or settings["mode"] != "payroll" or since is None or not budget["cashflow_enabled"]:
        return []
    today = clock.today()
    start = min(date.fromisoformat(budget["start_date"]), today)
    since = max(since, start)
    schedule = engine.payday_schedule(uid, since - timedelta(days=40), today)
    paydays = sorted(schedule["paydays"])
    overrides, confirmed = engine.income_overrides(uid, set(paydays))
    waiting = engine.unconfirmed_payday(paydays, confirmed, start=start, today=today,
                                        confirm_from=budget["payday_confirm_from"])
    hints = []
    for day in paydays:
        key = (day + timedelta(days=1)).isoformat()
        if day < since or day > today or day == waiting or key in recorded:
            continue
        value = overrides.get(day, schedule["paydays"][day]["amount"])
        if value <= 0:
            continue
        hints.append({
            "type": "payroll_income",
            "period_start": key,
            "payday": day.isoformat(),
            "title": ", ".join(schedule["paydays"][day]["titles"]) or "Выплата",
            "amount": amount(value),
            "confirmed": day in confirmed,
            "account_id": main_id,
        })
    return hints
