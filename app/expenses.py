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


_UNCHANGED = object()


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


def expense_for_user(user_id: int, transaction_id: int) -> dict | None:
    """Return one ordinary expense together with its current category."""
    with connect() as con:
        row = con.execute(
            "SELECT t.id,t.type,t.amount,t.tx_date,t.category_id,t.note,"
            "c.title AS category_title,c.emoji AS category_emoji "
            "FROM transactions t LEFT JOIN categories c "
            "ON c.id=t.category_id AND c.user_id=t.user_id "
            "WHERE t.id=? AND t.user_id=? AND t.type='expense' "
            "AND t.bill_rule_id IS NULL",
            (transaction_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def update_expense(
    user_id: int,
    transaction_id: int,
    *,
    amount: float | object = _UNCHANGED,
    category_id: int | None | object = _UNCHANGED,
    note: str | object = _UNCHANGED,
) -> dict:
    """Change one ordinary expense without creating a second transaction."""
    current = expense_for_user(user_id, transaction_id)
    if current is None:
        raise ValueError("Трата не найдена")

    new_amount = float(current["amount"])
    if amount is not _UNCHANGED:
        amount_cents = cents(amount)
        if amount_cents <= 0:
            raise ValueError("Сумма должна быть больше нуля")
        new_amount = money_amount(amount_cents)

    new_category_id = current["category_id"]
    if category_id is not _UNCHANGED:
        if category_id is not None:
            with connect() as con:
                category = con.execute(
                    "SELECT 1 FROM categories WHERE id=? AND user_id=?",
                    (category_id, user_id),
                ).fetchone()
            if category is None:
                raise ValueError("Категория не найдена")
        new_category_id = category_id

    new_note = current["note"]
    if note is not _UNCHANGED:
        new_note = str(note).strip()
        if len(new_note) > 200:
            raise ValueError("Комментарий не должен быть длиннее 200 символов")

    with connect() as con:
        con.execute(
            "UPDATE transactions SET amount=?,category_id=?,note=? "
            "WHERE id=? AND user_id=? AND type='expense' AND bill_rule_id IS NULL",
            (new_amount, new_category_id, new_note, transaction_id, user_id),
        )
    invalidate_current_auto_reserve(user_id)
    updated = expense_for_user(user_id, transaction_id)
    if updated is None:
        raise ValueError("Трата не найдена")
    return updated


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
