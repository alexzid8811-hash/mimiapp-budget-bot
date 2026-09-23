"""HTTP API for the card / buffer / piggy-bank model.

All numbers come from :mod:`app.engine`; this module only validates requests
and stores operations.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import Field

from . import payroll_app as base
from . import clock, engine
from .validation import APIModel
from .savings import check_piggy_history
from .auth import TelegramUser, current_user
from .money import cents


app = base.app
legacy = base.legacy


class CashflowSettingsIn(APIModel):
    cashflow_enabled: bool = False
    start_date: date
    start_capital: float = Field(default=0, ge=0)


class PiggyBankMovementIn(APIModel):
    amount: float = Field(gt=0)
    movement_date: date = Field(default_factory=lambda: clock.today())
    note: str = Field(default="", max_length=160)
    source: Literal["external", "daily_budget"] = "external"


class PiggyToCardIn(APIModel):
    amount: float = Field(gt=0)
    movement_date: date = Field(default_factory=lambda: clock.today())
    note: str = Field(default="", max_length=160)
    # "cover_overspend" pays for today's overspend: the remaining days keep
    # their limit.  "transfer" adds money to the period and is spread over the
    # remaining days.
    purpose: Literal["transfer", "cover_overspend"] = "transfer"


class CashflowIncomeOverrideIn(APIModel):
    amount: float = Field(ge=0)


def cashflow_settings(user_id: int) -> dict:
    return engine.settings_for(user_id)


def cashflow_snapshot(user_id: int) -> dict:
    """Budget snapshot for an enabled start capital, otherwise a stub."""
    settings = engine.settings_for(user_id)
    if not settings["cashflow_enabled"]:
        return {"enabled": False, "settings": settings}
    return engine.compute(user_id)


def piggy_bank_balance(user_id: int, through: date | None = None) -> float:
    sql = (
        "SELECT COALESCE(SUM(CASE WHEN direction='deposit' THEN amount ELSE -amount END),0) AS balance "
        "FROM piggy_bank_movements WHERE user_id=?"
    )
    params: tuple = (user_id,)
    if through is not None:
        sql += " AND movement_date<=?"
        params = (user_id, through.isoformat())
    row = legacy.one(sql, params)
    return round(float(row["balance"]) if row else 0.0, 2)


def piggy_bank_snapshot(user_id: int, limit: int = 80) -> dict:
    movements = legacy.rows(
        "SELECT id,direction,amount,movement_date,note,source,purpose,created_at FROM piggy_bank_movements "
        "WHERE user_id=? ORDER BY movement_date DESC,id DESC LIMIT ?",
        (user_id, min(max(limit, 1), 300)),
    )
    return {"balance": piggy_bank_balance(user_id), "movements": movements}


@app.get("/api/cashflow-settings")
def get_cashflow_settings(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    return cashflow_settings(uid)


@app.put("/api/cashflow-settings")
def save_cashflow_settings(payload: CashflowSettingsIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    if payload.start_date > clock.today():
        raise HTTPException(422, "Дата старта не может быть в будущем")
    with legacy.connect() as con:
        con.execute(
            "UPDATE settings SET cashflow_enabled=?,cashflow_start_date=?,cashflow_start_capital=?,"
            "initial_vacation_reserve=0,updated_at=CURRENT_TIMESTAMP "
            "WHERE user_id=?",
            (int(payload.cashflow_enabled), payload.start_date.isoformat(), payload.start_capital, uid),
        )
        con.execute("DELETE FROM reserve_movements WHERE user_id=? AND source='auto'", (uid,))
    # A new start point is a new history: every card period is recalculated.
    engine.forget_allocations(uid)
    return cashflow_settings(uid)


@app.get("/api/cashflow")
def get_cashflow(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    return cashflow_snapshot(uid)


BUFFER_KEYS = (
    "today", "horizon_end", "daily_target", "available_today", "buffer_balance",
    "capital_shortfall", "shortfall_date", "periods", "card_balance", "tomorrow_limit",
)


@app.get("/api/buffer")
def get_buffer(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    snapshot = cashflow_snapshot(uid)
    if not snapshot.get("enabled"):
        return {"enabled": False, "settings": snapshot["settings"], "periods": []}
    return {"enabled": True, "settings": snapshot["settings"], **{k: snapshot[k] for k in BUFFER_KEYS}}


def editable_payday_row(user_id: int, period_start: date) -> dict | None:
    snapshot = engine.compute(user_id, force_enabled=bool(cashflow_settings(user_id)["cashflow_enabled"]))
    return next(
        (row for row in snapshot["periods"]
         if row["override_key"] == period_start.isoformat() and row["received_editable"]),
        None,
    )


@app.put("/api/cashflow/income-overrides/{period_start}")
def save_cashflow_income_override(
    period_start: date,
    payload: CashflowIncomeOverrideIn,
    user: TelegramUser = Depends(current_user),
) -> dict:
    """Actual amount of the salary/advance that funds the card period
    starting on ``period_start``.  It replaces the forecast for this and every
    later calculation; closed periods are not editable."""
    uid = legacy.user_ready(user)
    if editable_payday_row(uid, period_start) is None:
        raise HTTPException(422, "Фактическую сумму можно ввести только для текущей или будущей выплаты")
    with legacy.connect() as con:
        con.execute(
            "INSERT INTO cashflow_income_overrides(user_id,period_start,amount) VALUES(?,?,?) "
            "ON CONFLICT(user_id,period_start) DO UPDATE SET "
            "amount=excluded.amount,updated_at=CURRENT_TIMESTAMP",
            (uid, period_start.isoformat(), payload.amount),
        )
    # The actual payment changes the periods funded from this payday on;
    # earlier periods keep the money they already received.
    engine.forget_allocations(uid, period_start - timedelta(days=1))
    return get_buffer(user)


@app.delete("/api/cashflow/income-overrides/{period_start}")
def delete_cashflow_income_override(
    period_start: date, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = legacy.user_ready(user)
    if editable_payday_row(uid, period_start) is None:
        raise HTTPException(422, "Выплата закрытого периода не изменяется")
    with legacy.connect() as con:
        deleted = con.execute(
            "DELETE FROM cashflow_income_overrides WHERE user_id=? AND period_start=?",
            (uid, period_start.isoformat()),
        )
    if deleted.rowcount == 0:
        raise HTTPException(404, "Корректировка выплаты не найдена")
    engine.forget_allocations(uid, period_start - timedelta(days=1))
    return get_buffer(user)


@app.get("/api/piggy-bank")
def get_piggy_bank(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    return piggy_bank_snapshot(uid)


def _insert_piggy(user_id: int, direction: str, amount: float, day: date, note: str,
                  source: str, purpose: str | None = None) -> dict:
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        movement_id = con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source,purpose) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, direction, amount, day.isoformat(), note, source, purpose),
        ).lastrowid
        try:
            check_piggy_history(con, user_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return legacy.one(
        "SELECT id,direction,amount,movement_date,note,source,purpose,created_at "
        "FROM piggy_bank_movements WHERE id=? AND user_id=?",
        (movement_id, user_id),
    ) or {}


def add_piggy_bank_movement(
    user_id: int, direction: Literal["deposit", "withdraw"], payload: PiggyBankMovementIn
) -> dict:
    if payload.movement_date > clock.today():
        raise HTTPException(422, "Дата операции не может быть в будущем")
    if direction == "withdraw" and payload.source == "daily_budget":
        raise HTTPException(422, "Перевод на карту выполняется операцией «Копилка → карта»")
    if direction == "deposit" and payload.source == "daily_budget":
        if payload.movement_date != clock.today():
            raise HTTPException(422, "Перенести можно только остаток сегодняшнего дня")
        flow = engine.compute(user_id, force_enabled=bool(cashflow_settings(user_id)["cashflow_enabled"]))
        if cents(payload.amount) > max(0, cents(flow["available_today"])):
            raise HTTPException(422, "Нельзя перенести больше неизрасходованного остатка за день")
    return _insert_piggy(user_id, direction, payload.amount, payload.movement_date,
                         payload.note, payload.source)


@app.post("/api/piggy-bank/deposit")
def deposit_to_piggy_bank(
    payload: PiggyBankMovementIn, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = legacy.user_ready(user)
    movement = add_piggy_bank_movement(uid, "deposit", payload)
    return {"movement": movement, **piggy_bank_snapshot(uid)}


@app.post("/api/piggy-bank/withdraw")
def withdraw_from_piggy_bank(
    payload: PiggyBankMovementIn, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = legacy.user_ready(user)
    movement = add_piggy_bank_movement(uid, "withdraw", payload)
    return {"movement": movement, **piggy_bank_snapshot(uid)}


@app.post("/api/piggy-bank/to-card")
def piggy_bank_to_card(payload: PiggyToCardIn, user: TelegramUser = Depends(current_user)) -> dict:
    """Explicit piggy bank -> card transfer.  The piggy bank decreases and the
    current card period increases; the buffer is never touched."""
    uid = legacy.user_ready(user)
    if payload.movement_date != clock.today():
        raise HTTPException(422, "Перевод на карту выполняется сегодняшним днём")
    if payload.purpose == "cover_overspend":
        flow = engine.compute(uid, force_enabled=bool(cashflow_settings(uid)["cashflow_enabled"]))
        if cents(payload.amount) > cents(flow["overspend"]):
            raise HTTPException(422, "Сумма больше сегодняшнего перерасхода")
    note = payload.note or ("Покрытие перерасхода" if payload.purpose == "cover_overspend" else "Перевод на карту")
    movement = _insert_piggy(uid, "withdraw", payload.amount, payload.movement_date, note,
                             "daily_budget", payload.purpose)
    return {"movement": movement, **piggy_bank_snapshot(uid)}


@app.delete("/api/piggy-bank/movements/{movement_id}")
def delete_piggy_bank_movement(
    movement_id: int, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        deleted = con.execute("DELETE FROM piggy_bank_movements WHERE id=? AND user_id=?", (movement_id, uid))
        if deleted.rowcount == 0:
            raise HTTPException(404, "Операция копилки не найдена")
        try:
            check_piggy_history(con, uid)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"ok": True, **piggy_bank_snapshot(uid)}


from .backup import router as backup_router

app.include_router(backup_router)
