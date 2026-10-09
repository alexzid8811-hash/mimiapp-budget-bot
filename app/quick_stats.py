"""Numbers for the bot's quick menu: the same as on the home screen of the
mini app, in both budget modes."""
from __future__ import annotations

from . import engine, irregular_engine
from .db import connect, user_db_path


def _registered(user_id: int) -> bool:
    """The user has opened the mini app at least once.  The file is checked
    before connect(): for an unknown id it would create an empty database."""
    if not user_db_path(user_id).exists():
        return False
    with connect() as con:
        return con.execute("SELECT 1 FROM settings WHERE user_id=?", (user_id,)).fetchone() is not None


def is_irregular_user(user_id: int) -> bool:
    """The budget is in the irregular-income mode; False for an unknown user."""
    return _registered(user_id) and irregular_engine.is_irregular(user_id)


def _today_expenses(user_id: int, day: str, irregular: bool) -> tuple[list[dict], list[dict], list[dict]]:
    """Today's expenses split the way the budget counts them: everyday
    spending (in «Потрачено» of the home screen), mandatory payments and, in
    the irregular-income mode, expenses paid from the emergency reserve."""
    with connect() as con:
        rows = con.execute(
            "SELECT COALESCE(NULLIF(t.note,''),c.title,'Расход') AS title,t.amount,"
            "t.bill_rule_id IS NOT NULL AS is_bill,EXISTS(SELECT 1 FROM emergency_reserve_movements m "
            "WHERE m.user_id=t.user_id AND m.expense_transaction_id=t.id) AS from_reserve "
            "FROM transactions t LEFT JOIN categories c ON c.id=t.category_id AND c.user_id=t.user_id "
            "WHERE t.user_id=? AND t.type='expense' AND t.tx_date=? ORDER BY t.id",
            (user_id, day),
        ).fetchall()
    everyday, bills, from_reserve = [], [], []
    for row in rows:
        item = {"title": row["title"], "amount": float(row["amount"])}
        if row["is_bill"]:
            bills.append(item)
        elif irregular and row["from_reserve"]:
            # The irregular mode does not count them in today's spending.
            from_reserve.append(item)
        else:
            everyday.append(item)
    return everyday, bills, from_reserve


def quick_numbers(user_id: int) -> dict | None:
    """Today's numbers of the active budget mode, or None when the user has
    never opened the mini app.  In the irregular mode «configured» is False
    until the mode has a start."""
    if not _registered(user_id):
        return None
    if irregular_engine.is_irregular(user_id):
        flow = irregular_engine.snapshot(user_id)
        if not flow.get("configured"):
            return {"mode": "irregular", "configured": False}
        numbers = {
            "mode": "irregular",
            "configured": True,
            "today": flow["today"],
            "free_balance": float(flow["free_balance"]),
            "stretch_until": flow["stretch_until"],
            "days_left": int(flow["days_left"]),
            # The same warning as on the home screen of this mode.
            "no_income_warning": bool(flow["no_income_warning"]),
            "days_without_income": int(flow["days_without_income"]),
        }
    else:
        # The same calculation as the home screen and the morning report.
        flow = engine.compute(user_id)
        numbers = {
            "mode": "payroll",
            "configured": True,
            "today": flow["today"],
            "card_balance": float(flow["remaining_period"]),
            "period_end": flow["period"]["end"],
            "period_days_left": int(flow["period"]["days_left"]),
            "payday_waiting": flow.get("payday_waiting"),
        }
    everyday, bills, from_reserve = _today_expenses(user_id, numbers["today"], numbers["mode"] == "irregular")
    numbers.update({
        "today_target": float(flow["today_target"]),
        "available_today": float(flow["available_today"]),
        "spent_today": float(flow["spent_today"]),
        "overspend": float(flow["overspend"]),
        "expenses": everyday,
        "bills_paid": bills,
        "reserve_paid": from_reserve,
    })
    return numbers
