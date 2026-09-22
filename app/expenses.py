"""Shared operations for ordinary expenses.

Both the Mini App API and the Telegram bot use this module. Keeping the
write path in one place prevents a chat entry from bypassing the budget
recalculation that follows an expense entered in the Mini App.
"""
from __future__ import annotations

from datetime import date

from . import clock, planning
from .db import connect
from .money import amount as money_amount
from .money import cents


def categories_for_user(user_id: int) -> list[dict]:
    """Return the user's categories in the order shown in the Mini App."""
    with connect() as con:
        return [
            dict(row)
            for row in con.execute(
                "SELECT id,title,emoji,sort_order FROM categories "
                "WHERE user_id=? ORDER BY sort_order,id",
                (user_id,),
            ).fetchall()
        ]


def invalidate_current_auto_reserve(user_id: int) -> None:
    """Discard the calculated reserve so it is rebuilt with the new expense."""
    period = planning.user_period(user_id, clock.today())
    with connect() as con:
        con.execute(
            "DELETE FROM reserve_movements WHERE user_id=? "
            "AND source='auto' AND period_start>=?",
            (user_id, period.start.isoformat()),
        )


def create_expense(
    user_id: int,
    amount: float,
    category_id: int | None,
    *,
    tx_date: date | None = None,
    note: str = "",
) -> dict:
    """Store one ordinary expense and make the current budget recalculate.

    A category is optional for the existing Mini App API, but when it is
    supplied it must belong to the same Telegram user. Amounts are stored in
    whole kopecks so both entry points use the same rounding.
    """
    tx_date = tx_date or clock.today()
    if tx_date > clock.today():
        raise ValueError("Дата операции не может быть в будущем")

    amount_cents = cents(amount)
    if amount_cents <= 0:
        raise ValueError("Сумма должна быть больше нуля")

    note = note.strip()
    if len(note) > 200:
        raise ValueError("Комментарий не должен быть длиннее 200 символов")

    normalized_amount = money_amount(amount_cents)
    with connect() as con:
        category = None
        if category_id is not None:
            category = con.execute(
                "SELECT id,title,emoji FROM categories WHERE id=? AND user_id=?",
                (category_id, user_id),
            ).fetchone()
            if category is None:
                raise ValueError("Категория не найдена")

        cur = con.execute(
            "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note,income_destination) "
            "VALUES(?, 'expense', ?, ?, ?, ?, 'daily')",
            (user_id, normalized_amount, tx_date.isoformat(), category_id, note),
        )
        transaction_id = int(cur.lastrowid)

    invalidate_current_auto_reserve(user_id)
    return {
        "id": transaction_id,
        "type": "expense",
        "amount": normalized_amount,
        "tx_date": tx_date.isoformat(),
        "category_id": category_id,
        "category_title": category["title"] if category else None,
        "category_emoji": category["emoji"] if category else None,
        "note": note,
    }


def delete_expense(user_id: int, transaction_id: int) -> bool:
    """Remove an ordinary expense created by the owner, for the chat undo action."""
    with connect() as con:
        cur = con.execute(
            "DELETE FROM transactions WHERE id=? AND user_id=? AND type='expense' "
            "AND bill_rule_id IS NULL",
            (transaction_id, user_id),
        )
    if cur.rowcount == 0:
        return False
    invalidate_current_auto_reserve(user_id)
    return True
