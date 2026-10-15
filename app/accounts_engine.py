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
    }
