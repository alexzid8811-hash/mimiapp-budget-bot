"""Persistence and audit trail for the user's separate buffer account.

The cash-flow forecast can suggest how much should be protected, but it must
not silently move money between a user's card and their real buffer account.
This module keeps that account balance explicit and records every deliberate
change.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Literal

from .db import connect
from .money import amount, cents


class BufferAccountError(ValueError):
    """A requested account movement would make the balance inconsistent."""


MovementSource = Literal["card_transfer", "income", "adjustment"]
Direction = Literal["deposit", "withdraw"]


def _configured_balance(con: sqlite3.Connection, user_id: int) -> float | None:
    row = con.execute(
        "SELECT buffer_account_balance FROM settings WHERE user_id=?", (user_id,)
    ).fetchone()
    if row is None or row["buffer_account_balance"] is None:
        return None
    return amount(cents(row["buffer_account_balance"]))


def balance_for_user(user_id: int) -> float | None:
    with connect() as con:
        return _configured_balance(con, user_id)


def movements_for_user(user_id: int, limit: int = 80) -> list[dict]:
    with connect() as con:
        return [
            dict(row)
            for row in con.execute(
                "SELECT id,direction,amount,movement_date,note,source,"
                "income_transaction_id,created_at FROM buffer_account_movements "
                "WHERE user_id=? ORDER BY movement_date DESC,id DESC LIMIT ?",
                (user_id, min(max(limit, 1), 300)),
            ).fetchall()
        ]


def _save_balance(con: sqlite3.Connection, user_id: int, value: float) -> float:
    normalized = amount(cents(value))
    if normalized < 0:
        raise BufferAccountError("В буфере недостаточно денег")
    con.execute(
        "UPDATE settings SET buffer_account_balance=?,updated_at=CURRENT_TIMESTAMP "
        "WHERE user_id=?",
        (normalized, user_id),
    )
    return normalized


def _record(
    con: sqlite3.Connection,
    user_id: int,
    direction: Direction,
    value: float,
    *,
    movement_date: date,
    note: str,
    source: MovementSource,
    income_transaction_id: int | None = None,
) -> None:
    con.execute(
        "INSERT INTO buffer_account_movements"
        "(user_id,direction,amount,movement_date,note,source,income_transaction_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            user_id,
            direction,
            amount(cents(value)),
            movement_date.isoformat(),
            note.strip()[:160],
            source,
            income_transaction_id,
        ),
    )


def set_actual_balance(
    con: sqlite3.Connection,
    user_id: int,
    value: float,
    *,
    movement_date: date,
    note: str = "",
) -> float:
    """Confirm or correct the real balance shown by the separate account."""
    desired = amount(cents(value))
    if desired < 0:
        raise BufferAccountError("Баланс буфера не может быть отрицательным")
    current = _configured_balance(con, user_id)
    if current is None:
        _save_balance(con, user_id, desired)
        if desired:
            _record(
                con,
                user_id,
                "deposit",
                desired,
                movement_date=movement_date,
                note=note or "Указан фактический остаток буфера",
                source="adjustment",
            )
        return desired

    delta = amount(cents(desired) - cents(current))
    if delta:
        _save_balance(con, user_id, desired)
        _record(
            con,
            user_id,
            "deposit" if delta > 0 else "withdraw",
            abs(delta),
            movement_date=movement_date,
            note=note or "Уточнён фактический остаток буфера",
            source="adjustment",
        )
    return desired


def transfer(
    con: sqlite3.Connection,
    user_id: int,
    direction: Direction,
    value: float,
    *,
    movement_date: date,
    note: str = "",
) -> float:
    """Move money between the card and buffer without changing total cash."""
    current = _configured_balance(con, user_id)
    if current is None:
        raise BufferAccountError("Сначала укажите фактический остаток буфера")
    transfer_value = amount(cents(value))
    if transfer_value <= 0:
        raise BufferAccountError("Сумма перевода должна быть больше нуля")
    if direction == "withdraw" and transfer_value > current:
        raise BufferAccountError("Нельзя вернуть с буфера больше его остатка")
    updated = _save_balance(
        con,
        user_id,
        current + transfer_value if direction == "deposit" else current - transfer_value,
    )
    _record(
        con,
        user_id,
        direction,
        transfer_value,
        movement_date=movement_date,
        note=note or (
            "Перевод с карты в буфер" if direction == "deposit" else "Перевод из буфера на карту"
        ),
        source="card_transfer",
    )
    return updated


def sync_income_destination(
    con: sqlite3.Connection,
    user_id: int,
    transaction_id: int,
    *,
    previous_amount: float = 0,
    new_amount: float = 0,
    movement_date: date,
    note: str = "",
) -> float | None:
    """Keep explicitly buffer-directed income in the physical account.

    Existing users remain on the legacy calculation until they confirm a real
    balance.  From that point onward a new income sent to the buffer is an
    explicit deposit, and changing/deleting that income reverses the same
    amount atomically.
    """
    current = _configured_balance(con, user_id)
    if current is None:
        return None

    previous = amount(cents(previous_amount))
    new = amount(cents(new_amount))
    updated = amount(cents(current) - cents(previous) + cents(new))
    if updated < 0:
        raise BufferAccountError(
            "Нельзя изменить или удалить этот доход: в буфере уже не хватает его суммы"
        )

    if updated != current:
        _save_balance(con, user_id, updated)
    con.execute(
        "DELETE FROM buffer_account_movements "
        "WHERE user_id=? AND income_transaction_id=?",
        (user_id, transaction_id),
    )
    if new:
        _record(
            con,
            user_id,
            "deposit",
            new,
            movement_date=movement_date,
            note=note or "Доход направлен в буфер",
            source="income",
            income_transaction_id=transaction_id,
        )
    return updated
