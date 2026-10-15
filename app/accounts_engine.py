"""Reads a user's accounts and operations, runs :mod:`app.accounts` and builds
the API snapshot.  The budget is only read here, never changed."""
from __future__ import annotations

from datetime import date, timedelta

from . import clock
from .accounts import (
    Account, AccountsInput, BudgetOp, Flow, Transfer, breakdown, calculate, transfer_hints,
)
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
        created=row["created_at"] or "",
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


def placement(con, uid: int, mode: str | None = None) -> tuple[dict[str, int], dict[str, tuple]]:
    """Account of every envelope of the mode (the main account when not set)
    and when each explicit assignment was made: (day, UTC timestamp)."""
    settings = settings_for(con, uid)
    mode = mode or settings["mode"]
    rows = {r["envelope"]: r for r in con.execute(
        "SELECT envelope,account_id,assigned_on,assigned_at FROM envelope_accounts WHERE user_id=?", (uid,))}
    main_id = settings["main_account_id"]
    places, since = {}, {}
    for key in ENVELOPES[mode]:
        row = rows.get(key)
        if row is not None:
            places[key] = int(row["account_id"])
            since[key] = (date.fromisoformat(row["assigned_on"]), row["assigned_at"] or "")
        elif main_id is not None:
            places[key] = int(main_id)
    return places, since


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


def _payroll_actual(uid: int):
    """The ledger of today's actual balances, as engine.compute builds it
    (without an unconfirmed payday)."""
    from dataclasses import replace

    from . import engine
    from .ledger import run_ledger

    inp, extras, _today, _start, _settings, _enabled = engine._run(uid)
    actual = run_ledger(inp)
    waiting = extras["waiting"]
    if waiting:
        without = dict(inp.buffer_in)
        without[waiting] = without.get(waiting, 0) - extras["payday_amount"].get(waiting, 0)
        actual = run_ledger(replace(inp, buffer_in=without, unconfirmed_payday=waiting))
    return inp, actual


def _expense_envelope(row) -> str | None:
    funding = row["funding_envelope"]
    if funding:
        return None if funding == "none" else funding
    if row["reserve_linked"]:
        return "reserve"
    if row["piggy_linked"]:
        return "piggy"
    return "spending"


def budget_ops(uid: int, mode: str) -> list[BudgetOp]:
    """What the budget did to the envelopes, with what happened on the
    accounts at the same time.  Explicit operations of the piggy bank and the
    reserve are left out: they create their own transfers at once."""
    today = clock.today()
    ops: list[BudgetOp] = []
    splits: dict = {}
    irregular_input = None
    if mode == "irregular":
        from . import irregular_engine

        ran = irregular_engine.run(uid)
        if ran is None:
            return []
        irregular_input, result, _settings = ran
        splits = result.splits
    with connect() as con:
        remainders = {r["bill_payment_id"]: r for r in con.execute(
            "SELECT p.id,p.amount,p.bill_payment_id,t.from_account_id,t.to_account_id,t.amount AS moved "
            "FROM piggy_bank_movements p LEFT JOIN account_transfers t ON t.piggy_movement_id=p.id "
            "AND t.user_id=p.user_id WHERE p.user_id=? AND p.bill_payment_id IS NOT NULL", (uid,))}
        rows = [dict(r) for r in con.execute(
            "SELECT t.id,t.type,t.amount,t.tx_date,t.account_id,t.bill_rule_id,t.bill_due_date,t.created_at,"
            "t.bill_planned_amount,t.income_destination,t.funding_envelope,a.kind AS account_kind,"
            "EXISTS(SELECT 1 FROM emergency_reserve_movements m WHERE m.user_id=t.user_id "
            "AND m.expense_transaction_id=t.id) AS reserve_linked,"
            "EXISTS(SELECT 1 FROM piggy_bank_movements m WHERE m.user_id=t.user_id "
            "AND m.expense_transaction_id=t.id) AS piggy_linked "
            "FROM transactions t JOIN accounts a ON a.id=t.account_id AND a.user_id=t.user_id "
            "WHERE t.user_id=? AND t.tx_date<=?", (uid, today.isoformat()))]
        payroll_incomes = [] if mode != "payroll" else [dict(r) for r in con.execute(
            "SELECT account_id,amount,date,created_at FROM account_incomes WHERE user_id=? AND source='payroll' "
            "AND date<=?",
            (uid, today.isoformat()))]
        credit_payments = [dict(r) for r in con.execute(
            "SELECT p.from_account_id,p.amount,p.date,p.funding_envelope,p.transaction_id,p.created_at,"
            "t.bill_planned_amount AS planned FROM account_transfers p "
            "LEFT JOIN transactions t ON t.id=p.transaction_id AND t.user_id=p.user_id "
            "WHERE p.user_id=? AND p.kind='credit_payment' AND p.date<=?", (uid, today.isoformat()))]

    for r in payroll_incomes:
        value = cents(r["amount"])
        ops.append(BudgetOp(date.fromisoformat(r["date"]), {"buffer": value}, {int(r["account_id"]): value},
                            r["created_at"]))
    bill_payments = {}
    for r in rows:
        day = date.fromisoformat(r["tx_date"])
        value = cents(r["amount"])
        account_id = int(r["account_id"])
        if r["type"] == "income":
            if mode == "irregular":
                split = splits.get(r["id"])
                if split is None:
                    continue
                envelopes = {"reserve": split.reserve, "buffer": split.bills, "spending": split.free,
                             "piggy": split.piggy}
            else:
                destination = r["income_destination"] or "daily"
                key = ("buffer" if destination in ("buffer", "reserve")
                       else "piggy" if destination == "piggy" else "spending")
                envelopes = {key: value}
            ops.append(BudgetOp(day, envelopes, {account_id: value}, r["created_at"]))
        elif r["bill_rule_id"] is not None:
            bill_payments[r["id"]] = r
        elif r["account_kind"] != "credit":
            envelope = _expense_envelope(r)
            if envelope is not None:
                ops.append(BudgetOp(day, {envelope: -value}, {account_id: -value}, r["created_at"]))

    put_aside = _irregular_put_aside(irregular_input, bill_payments) if mode == "irregular" else {}
    for payment_id, r in bill_payments.items():
        day = date.fromisoformat(r["tx_date"])
        paid = cents(r["amount"])
        planned = cents(r["bill_planned_amount"]) if r["bill_planned_amount"] is not None else paid
        released = put_aside.get(payment_id, planned) if mode == "irregular" else planned
        remainder = remainders.get(payment_id)
        to_piggy = cents(remainder["amount"]) if remainder else 0
        physical = {int(r["account_id"]): -paid}
        if remainder and remainder["from_account_id"] is not None:
            moved = cents(remainder["moved"])
            physical[int(remainder["from_account_id"])] = physical.get(int(remainder["from_account_id"]), 0) - moved
            physical[int(remainder["to_account_id"])] = physical.get(int(remainder["to_account_id"]), 0) + moved
        envelopes = {"buffer": -released, "spending": released - paid - to_piggy, "piggy": to_piggy}
        ops.append(BudgetOp(day, envelopes, physical, r["created_at"]))

    for r in credit_payments:
        envelope = r["funding_envelope"]
        value = cents(r["amount"])
        if envelope in ("spending", "piggy", "reserve") and r["transaction_id"] is None:
            envelopes = {envelope: -value}
        elif envelope == "buffer" and r["transaction_id"] is not None:
            # The planned repayment: the buffer releases what it put aside,
            # the difference belongs to the daily money (like any bill).
            planned = cents(r["planned"]) if r["planned"] is not None else value
            envelopes = {"buffer": -planned, "spending": planned - value}
        else:
            continue
        ops.append(BudgetOp(date.fromisoformat(r["date"]), envelopes, {int(r["from_account_id"]): -value},
                            r["created_at"]))

    if mode == "payroll":
        from . import engine

        if engine.settings_for(uid)["cashflow_enabled"]:
            inp, actual = _payroll_actual(uid)
            for period in actual.periods:
                value = actual.allocations.get(period.start, 0)
                if period.funded_on <= today and value:
                    ops.append(BudgetOp(period.funded_on, {"buffer": -value, "spending": value}, {}))
    return ops


def _irregular_put_aside(inp, payments: dict) -> dict[int, int]:
    """Money released from «На обязательные» by every bill payment: what was
    put aside for that occurrence on the morning of the payment."""
    if inp is None or not payments:
        return {}
    from dataclasses import replace

    from .irregular import replay

    result = {}
    for bill in inp.bills:
        if bill.payment_id not in payments or bill.paid_on is None or bill.paid_on < inp.start:
            continue
        unpaid = [b if b.key != bill.key else replace(b, paid_on=None, actual=0, to_piggy=0, payment_id=None)
                  for b in inp.bills]
        before = replay(replace(inp, today=bill.paid_on, bills=unpaid))
        result[bill.payment_id] = before.bills_fund.get(bill.key, 0)
    return result


def plain_transfers(con, uid: int) -> list[Transfer]:
    return [
        Transfer(int(r["from_account_id"]), r["to_account_id"], date.fromisoformat(r["date"]), cents(r["amount"]),
                 created=r["created_at"])
        for r in con.execute(
            "SELECT from_account_id,to_account_id,amount,date,created_at FROM account_transfers "
            "WHERE user_id=? AND kind IN ('manual','hint')", (uid,))
    ]


def snapshot(uid: int, *, with_amounts: bool = False) -> dict:
    """``with_amounts`` reads the envelopes even while the feature is off
    (the setup wizard fills the opening balances with them)."""
    with connect() as con:
        settings = settings_for(con, uid)
        rows = account_rows(con, uid)
        accounts_input = build_input(con, uid, rows)
        result = calculate(accounts_input)
        places, assigned_on = placement(con, uid)
        transfers = plain_transfers(con, uid)
        operations = {int(r["id"]): has_operations(con, uid, int(r["id"])) for r in rows}
        scheduled = scheduled_repayments(con, uid)
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
            "account_id": places.get(key),
            "assigned_on": assigned_on[key][0].isoformat() if key in assigned_on else None,
            "amount": amount(amounts[key]) if amounts is not None else None,
        }
        for key in keys
    ]
    hints, parts = [], {}
    if settings["enabled"] and amounts is not None:
        found = transfer_hints(accounts_input.accounts, places, assigned_on, budget_ops(uid, mode), transfers)
        hints = [{"type": "transfer", "from_account_id": h.from_id, "to_account_id": h.to_id,
                  "amount": amount(h.amount)} for h in found]
        parts = breakdown(result.states, places, amounts, found, keys)
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
            "breakdown": _breakdown_dict(parts.get(int(row["id"])), mode),
            **(_credit_dict(row, state, scheduled.get(int(row["id"]))) if row["kind"] == "credit" else {}),
        })
    debts = [a for a in accounts if a["kind"] == "credit" and (a["debt"] or 0) > 0]
    due_dates = sorted(a["due_date"] for a in debts if a.get("due_date"))
    return {
        "enabled": settings["enabled"],
        "today": clock.today().isoformat(),
        "mode": mode,
        "main_account_id": main_id,
        "budget_configured": amounts is not None,
        "total": amount(result.total),
        "credit_debt": amount(result.credit_debt),
        # The nearest repayment date of a card with a debt.
        "credit_due_date": due_dates[0] if due_dates else None,
        "accounts": accounts,
        "envelopes": envelopes,
        "operations_without_account": pending,
        "hints": (payroll_income_hints(uid) + hints) if settings["enabled"] else [],
    }


def _credit_dict(row: dict, state, scheduled: dict | None) -> dict:
    """Repayment date and the planned repayment (with a warning when the debt
    changed after it was planned)."""
    payment = next_payment_date(row["payment_day"], clock.today())
    due = scheduled["due_date"] if scheduled else (payment.isoformat() if payment else None)
    debt = amount(state.debt)
    return {
        "next_payment_date": payment.isoformat() if payment else None,
        "due_date": due,
        "scheduled": {**scheduled, "outdated": abs(scheduled["amount"] - debt) >= 0.005} if scheduled else None,
    }


def _breakdown_dict(part, mode: str) -> dict | None:
    if part is None:
        return None
    return {
        "envelopes": [{"key": k, "label": ENVELOPE_LABELS[mode][k], "amount": amount(v)}
                      for k, v in part.envelopes.items()],
        "envelopes_total": amount(part.envelopes_total),
        "outgoing_hints": amount(part.outgoing_hints),
        "difference": amount(part.difference),
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
    # Bank interest is an expense in the analytics («Проценты банка»); the
    # budget is not touched either way.
    tx_id = (interest_expense(con, uid, amount(-difference), day.isoformat(),
                              f"Проценты по кредитке {account['title']}") if reason == "interest" else None)
    return con.execute(
        "INSERT INTO account_adjustments(user_id,account_id,amount,date,reason,note,transaction_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (uid, account["id"], amount(difference), day.isoformat(), reason, note, tx_id),
    ).lastrowid


def delete_adjustment(con, uid: int, row: dict) -> None:
    con.execute("DELETE FROM account_adjustments WHERE id=? AND user_id=?", (row["id"], uid))
    if row.get("transaction_id"):
        con.execute("DELETE FROM transactions WHERE id=? AND user_id=?", (row["transaction_id"], uid))


def delete_transfer(con, uid: int, row: dict) -> None:
    if row["kind"] == "credit_payment":
        return delete_credit_payment(con, uid, row)
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
            "deletable": r["kind"] in ("manual", "hint", "reassign")
            or (r["kind"] == "credit_payment" and r["transaction_id"] is None),
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


# ---------- Transfers linked to explicit envelope operations ----------


def _linked(con, uid: int, source: int | None, target: int | None, value: float, day: str, *,
            piggy: int | None = None, reserve: int | None = None, outside: bool = False) -> None:
    """A transfer created together with an operation of the piggy bank or the
    reserve and deleted with it.  Nothing when both envelopes share an account."""
    if source is None or (target is None and not outside) or source == target or not value:
        return
    con.execute(
        "INSERT INTO account_transfers(user_id,from_account_id,to_account_id,amount,date,kind,"
        "piggy_movement_id,reserve_movement_id) VALUES(?,?,?,?,?,'linked',?,?)",
        (uid, source, target, value, day, piggy, reserve),
    )


def link_piggy(con, uid: int, movement_id: int, *, direction: str, source: str, value: float, day: str,
               account_id: int | None = None, account_move: bool = True) -> None:
    """Accounts side of a piggy bank operation.  ``account_id``: where outside
    money came from (None: from outside the accounts) or where withdrawn money
    went (None: it left the accounts); ``account_move`` False: the money is
    already on the piggy bank's account («Добавить в копилку»)."""
    if not settings_for(con, uid)["enabled"]:
        return
    places, _ = placement(con, uid)
    piggy, spending = places.get("piggy"), places.get("spending")
    if source == "daily_budget":
        if direction == "deposit":
            _linked(con, uid, spending, piggy, value, day, piggy=movement_id)
        else:
            _linked(con, uid, piggy, spending, value, day, piggy=movement_id)
        return
    if not account_move or piggy is None:
        return
    if direction == "deposit":
        if account_id is None:
            con.execute(
                "INSERT INTO account_incomes(user_id,account_id,amount,date,source,piggy_movement_id) "
                "VALUES(?,?,?,?,'piggy_external',?)",
                (uid, piggy, value, day, movement_id),
            )
        else:
            _linked(con, uid, account_id, piggy, value, day, piggy=movement_id)
    else:
        _linked(con, uid, piggy, account_id, value, day, piggy=movement_id, outside=account_id is None)


def link_reserve(con, uid: int, movement_id: int, *, kind: str, value: float, day: str,
                 account_id: int | None = None, account_move: bool = True,
                 expense_id: int | None = None) -> None:
    """Accounts side of an operation of the emergency reserve."""
    if not settings_for(con, uid)["enabled"]:
        return
    places, _ = placement(con, uid)
    reserve, spending = places.get("reserve"), places.get("spending")
    if kind == "from_free":
        _linked(con, uid, spending, reserve, value, day, reserve=movement_id)
    elif kind in ("to_free", "cover_overspend"):
        _linked(con, uid, reserve, spending, value, day, reserve=movement_id)
    elif kind == "pay_expense" and expense_id is not None:
        con.execute(
            "UPDATE transactions SET account_id=?,funding_envelope='reserve' WHERE id=? AND user_id=?",
            (account_id or reserve, expense_id, uid),
        )
    elif kind == "deposit" and account_move and reserve is not None:
        if account_id is None:
            con.execute(
                "INSERT INTO account_incomes(user_id,account_id,amount,date,source,reserve_movement_id) "
                "VALUES(?,?,?,?,'reserve_external',?)",
                (uid, reserve, value, day, movement_id),
            )
        else:
            _linked(con, uid, account_id, reserve, value, day, reserve=movement_id)


def sync_bill_remainder(con, uid: int, payment_id: int) -> None:
    """The remainder of a paid bill moved to the piggy bank: from the account
    the bill was paid from to the piggy bank's account."""
    movement = con.execute(
        "SELECT id,amount,movement_date FROM piggy_bank_movements WHERE user_id=? AND bill_payment_id=?",
        (uid, payment_id),
    ).fetchone()
    if movement is None:
        return
    con.execute("DELETE FROM account_transfers WHERE user_id=? AND piggy_movement_id=?", (uid, movement["id"]))
    if not settings_for(con, uid)["enabled"]:
        return
    places, _ = placement(con, uid)
    paid_from = con.execute(
        "SELECT account_id FROM transactions WHERE id=? AND user_id=?", (payment_id, uid)
    ).fetchone()
    source = paid_from["account_id"] if paid_from and paid_from["account_id"] else places.get("buffer")
    _linked(con, uid, source, places.get("piggy"), movement["amount"], movement["movement_date"],
            piggy=movement["id"])


def sync_piggy_expense(con, uid: int, tx_id: int, funding: str | None, value: float, day: str, note: str) -> None:
    """An expense paid from the piggy bank keeps a withdrawal of the same
    amount and date (like an expense paid from the reserve)."""
    if funding == "piggy":
        updated = con.execute(
            "UPDATE piggy_bank_movements SET amount=?,movement_date=?,note=? "
            "WHERE user_id=? AND expense_transaction_id=?",
            (value, day, note[:160], uid, tx_id),
        )
        if updated.rowcount == 0:
            con.execute(
                "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source,"
                "expense_transaction_id) VALUES(?, 'withdraw', ?, ?, ?, 'external', ?)",
                (uid, value, day, note[:160], tx_id),
            )
    else:
        con.execute("DELETE FROM piggy_bank_movements WHERE user_id=? AND expense_transaction_id=?", (uid, tx_id))


def sync_reserve_expense(con, uid: int, tx_id: int, funding: str | None, value: float, day: str, note: str) -> None:
    """An expense paid from the emergency reserve («Из резерва»)."""
    if funding == "reserve":
        updated = con.execute(
            "UPDATE emergency_reserve_movements SET amount=?,movement_date=? "
            "WHERE user_id=? AND expense_transaction_id=?",
            (value, day, uid, tx_id),
        )
        if updated.rowcount == 0:
            con.execute(
                "INSERT INTO emergency_reserve_movements(user_id,direction,amount,movement_date,reason,kind,"
                "expense_transaction_id) VALUES(?, 'withdraw', ?, ?, ?, 'pay_expense', ?)",
                (uid, value, day, (note or "Трата из резерва")[:160], tx_id),
            )
    else:
        con.execute("DELETE FROM emergency_reserve_movements WHERE user_id=? AND expense_transaction_id=?",
                    (uid, tx_id))


# ---------- Credit cards ----------

INTEREST_CATEGORY = ("Проценты банка", "🏦")


def interest_category(con, uid: int) -> int:
    """The category of bank interest and fees, created on first use."""
    title, emoji = INTEREST_CATEGORY
    row = con.execute("SELECT id FROM categories WHERE user_id=? AND title=?", (uid, title)).fetchone()
    if row:
        return int(row["id"])
    order = con.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM categories WHERE user_id=?", (uid,)).fetchone()[0]
    return con.execute(
        "INSERT INTO categories(user_id,title,emoji,sort_order) VALUES(?,?,?,?)", (uid, title, emoji, order)
    ).lastrowid


def interest_expense(con, uid: int, value: float, day: str, note: str) -> int:
    """Bank interest as an expense for analytics only: outside the budget
    (funding 'none') and on no account (the repayment carries the money)."""
    return con.execute(
        "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note,funding_envelope) "
        "VALUES(?, 'expense', ?, ?, ?, ?, 'none')",
        (uid, value, day, interest_category(con, uid), note[:200]),
    ).lastrowid


def locked_transaction(con, uid: int, tx_id: int) -> str | None:
    """Why an expense cannot be edited on its own, or None."""
    if con.execute(
        "SELECT 1 FROM account_transfers WHERE user_id=? AND interest_transaction_id=?", (uid, tx_id)
    ).fetchone():
        return "Это проценты по погашению кредитки: измените погашение в истории кредитки"
    if con.execute(
        "SELECT 1 FROM account_adjustments WHERE user_id=? AND transaction_id=?", (uid, tx_id)
    ).fetchone():
        return "Это проценты из исправления долга: удалите исправление в истории кредитки"
    return None


def repay(con, uid: int, *, credit_id: int, source_id: int, value: float, interest: float, funding: str,
          day: str, note: str = "", transaction_id: int | None = None, title: str = "") -> int:
    """Repayment of a credit card: the money leaves ``source_id``, the debt
    falls by ``value - interest``, the envelope ``funding`` pays ``value``
    (the daily budget reads it from the transfer; the piggy bank and the
    reserve get a withdrawal of their own; the buffer pays through the bill)."""
    piggy_id = reserve_id = None
    reason = f"Погашение кредитки {title}".strip()
    if funding == "piggy":
        piggy_id = con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source) "
            "VALUES(?, 'withdraw', ?, ?, ?, 'external')",
            (uid, value, day, reason[:160]),
        ).lastrowid
    elif funding == "reserve":
        reserve_id = con.execute(
            "INSERT INTO emergency_reserve_movements(user_id,direction,amount,movement_date,reason,kind) "
            "VALUES(?, 'withdraw', ?, ?, ?, 'pay_expense')",
            (uid, value, day, reason[:160]),
        ).lastrowid
    interest_id = interest_expense(con, uid, interest, day, f"Проценты по кредитке {title}".strip()) if interest else None
    return con.execute(
        "INSERT INTO account_transfers(user_id,from_account_id,to_account_id,amount,date,note,kind,funding_envelope,"
        "interest_amount,piggy_movement_id,reserve_movement_id,transaction_id,interest_transaction_id) "
        "VALUES(?,?,?,?,?,?,'credit_payment',?,?,?,?,?,?)",
        (uid, source_id, credit_id, value, day, note, funding, interest, piggy_id, reserve_id, transaction_id,
         interest_id),
    ).lastrowid


def delete_credit_payment(con, uid: int, row: dict) -> None:
    if row["interest_transaction_id"]:
        con.execute("DELETE FROM transactions WHERE id=? AND user_id=?", (row["interest_transaction_id"], uid))
    # The envelope withdrawals go first; the transfer follows them (cascade).
    con.execute("DELETE FROM account_transfers WHERE id=? AND user_id=?", (row["id"], uid))
    if row["piggy_movement_id"]:
        con.execute("DELETE FROM piggy_bank_movements WHERE id=? AND user_id=?", (row["piggy_movement_id"], uid))
    if row["reserve_movement_id"]:
        con.execute("DELETE FROM emergency_reserve_movements WHERE id=? AND user_id=?",
                    (row["reserve_movement_id"], uid))


def next_payment_date(payment_day: int | None, today: date) -> date | None:
    if not payment_day:
        return None
    from .budget import add_months, month_date

    this_month = month_date(today.year, today.month, int(payment_day))
    if this_month >= today:
        return this_month
    following = add_months(today.replace(day=1), 1)
    return month_date(following.year, following.month, int(payment_day))


def scheduled_repayments(con, uid: int) -> dict[int, dict]:
    """The planned, still unpaid repayment of every credit card."""
    today = clock.today()
    rules = [dict(r) for r in con.execute(
        "SELECT id,amount,once_date,credit_account_id FROM bill_rules WHERE user_id=? AND archived=0 AND active=1 "
        "AND credit_account_id IS NOT NULL AND once_date IS NOT NULL", (uid,))]
    result = {}
    for rule in rules:
        due = date.fromisoformat(rule["once_date"])
        paid = con.execute(
            "SELECT 1 FROM transactions WHERE user_id=? AND bill_rule_id=? AND bill_due_date=?",
            (uid, rule["id"], rule["once_date"]),
        ).fetchone()
        if paid or due < today - timedelta(days=62):
            continue
        current = result.get(int(rule["credit_account_id"]))
        if current is None or rule["once_date"] < current["due_date"]:
            result[int(rule["credit_account_id"])] = {
                "bill_id": int(rule["id"]), "due_date": rule["once_date"], "amount": float(rule["amount"]),
            }
    return result
