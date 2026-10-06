"""HTTP API for the irregular-income mode.

All numbers come from :mod:`app.irregular_engine`; this module only
validates requests and stores operations.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from . import clock, irregular_engine as engine
from .auth import TelegramUser, current_user
from .db import connect, ensure_user
from .irregular import Split
from .money import cents
from .savings import check_piggy_history
from .validation import APIModel


router = APIRouter(prefix="/api", tags=["irregular"])


class BudgetModeIn(APIModel):
    budget_mode: Literal["payroll", "irregular"]
    start_date: date | None = None
    start_total: float | None = Field(default=None, ge=0)
    start_reserve: float | None = Field(default=None, ge=0)


class IrregularSettingsIn(APIModel):
    reserve_percent: float = Field(default=10, ge=0, le=100)
    reserve_target: float | None = Field(default=None, ge=0)
    stretch_days: int = Field(default=14, ge=3, le=60)
    lookahead_days: int = Field(default=30, ge=0, le=90)
    start_date: date | None = None
    start_total: float | None = Field(default=None, ge=0)
    start_reserve: float | None = Field(default=None, ge=0)


Destination = Literal["split", "reserve", "piggy"]


class IncomeIn(APIModel):
    amount: float = Field(gt=0)
    tx_date: date = Field(default_factory=lambda: clock.today())
    income_source: str = Field(default="", max_length=80)
    reserve_percent: float | None = Field(default=None, ge=0, le=100)
    destination: Destination = "split"
    note: str = Field(default="", max_length=200)


class PreviewIn(APIModel):
    amount: float = Field(gt=0)
    tx_date: date = Field(default_factory=lambda: clock.today())
    reserve_percent: float | None = Field(default=None, ge=0, le=100)
    destination: Destination = "split"
    exclude_id: int | None = None


class ReserveWithdrawIn(APIModel):
    amount: float = Field(gt=0)
    movement_date: date = Field(default_factory=lambda: clock.today())
    purpose: Literal["pay_expense", "to_free", "cover_overspend"]
    reason: str = Field(min_length=1, max_length=160)
    category_id: int | None = None


class ReserveDepositIn(APIModel):
    amount: float = Field(gt=0)
    movement_date: date = Field(default_factory=lambda: clock.today())
    kind: Literal["deposit", "from_free"] = "deposit"
    reason: str = Field(default="", max_length=160)


def _ready(user: TelegramUser) -> int:
    ensure_user(user.id, user.first_name, user.username)
    return user.id


def _start_values(current: dict, payload) -> tuple[str | None, float, float]:
    start = payload.start_date.isoformat() if payload.start_date else current["start_date"]
    total = payload.start_total if payload.start_total is not None else current["start_total"]
    reserve = payload.start_reserve if payload.start_reserve is not None else current["start_reserve"]
    if payload.start_date and payload.start_date > clock.today():
        raise HTTPException(422, "Дата старта не может быть в будущем")
    if cents(reserve) > cents(total):
        raise HTTPException(422, "Резерв на старте не может быть больше всех денег")
    return start, float(total), float(reserve)


def _save_start(uid: int, start: str | None, total: float, reserve: float) -> None:
    try:
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE settings SET irregular_start_date=?,irregular_start_total=?,irregular_start_reserve=?,"
                "updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (start, total, reserve, uid),
            )
            engine.check_reserve(con, uid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/irregular/settings")
def get_settings(user: TelegramUser = Depends(current_user)) -> dict:
    return engine.settings_for(_ready(user))


@router.put("/irregular/settings")
def save_settings(payload: IrregularSettingsIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    current = engine.settings_for(uid)
    start, total, reserve = _start_values(current, payload)
    target = payload.reserve_target if payload.reserve_target else None
    try:
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE settings SET irregular_reserve_percent=?,irregular_reserve_target=?,"
                "irregular_stretch_days=?,irregular_bills_lookahead_days=?,irregular_start_date=?,"
                "irregular_start_total=?,irregular_start_reserve=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (payload.reserve_percent, target, payload.stretch_days, payload.lookahead_days,
                 start, total, reserve, uid),
            )
            # A new target or start can change how much the reserve received.
            engine.check_reserve(con, uid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return engine.settings_for(uid)


@router.put("/budget-mode")
def save_budget_mode(payload: BudgetModeIn, user: TelegramUser = Depends(current_user)) -> dict:
    """Switch the mode.  The data of the other mode stays untouched."""
    uid = _ready(user)
    if payload.budget_mode == "irregular":
        current = engine.settings_for(uid)
        start, total, reserve = _start_values(current, payload)
        if not start:
            raise HTTPException(422, "Укажите дату старта и сколько денег у вас сейчас")
        _save_start(uid, start, total, reserve)
    with connect() as con:
        con.execute(
            "UPDATE settings SET budget_mode=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (payload.budget_mode, uid),
        )
    return engine.settings_for(uid)


@router.get("/irregular")
def get_snapshot(user: TelegramUser = Depends(current_user)) -> dict:
    return engine.snapshot(_ready(user))


def _configured(uid: int) -> dict:
    settings = engine.settings_for(uid)
    if not settings["start_date"]:
        raise HTTPException(422, "Сначала включите режим подработок и укажите старт")
    return settings


def _check_day(settings: dict, day: date, what: str) -> None:
    if day > clock.today():
        raise HTTPException(422, f"Дата {what} не может быть в будущем")
    if day < date.fromisoformat(settings["start_date"]):
        raise HTTPException(422, f"Дата {what} раньше даты старта режима")


@router.post("/irregular/preview")
def preview_income(payload: PreviewIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    settings = _configured(uid)
    percent = settings["reserve_percent"] if payload.reserve_percent is None else payload.reserve_percent
    try:
        return engine.preview(uid, value=payload.amount, day=payload.tx_date, percent=percent,
                              destination=payload.destination, exclude_id=payload.exclude_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/irregular/incomes")
def list_incomes(user: TelegramUser = Depends(current_user)) -> list[dict]:
    return engine.incomes(_ready(user))


def _write_income(uid: int, payload: IncomeIn, tx_id: int | None = None) -> int:
    settings = _configured(uid)
    _check_day(settings, payload.tx_date, "дохода")
    percent = settings["reserve_percent"] if payload.reserve_percent is None else payload.reserve_percent
    values = (payload.amount, payload.tx_date.isoformat(), payload.note, payload.destination,
              percent, payload.income_source.strip())
    try:
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if tx_id is None:
                tx_id = con.execute(
                    "INSERT INTO transactions(user_id,type,amount,tx_date,note,income_destination,"
                    "reserve_percent,income_source) VALUES(?, 'income', ?, ?, ?, ?, ?, ?)",
                    (uid, *values),
                ).lastrowid
            else:
                cur = con.execute(
                    "UPDATE transactions SET amount=?,tx_date=?,note=?,income_destination=?,reserve_percent=?,"
                    "income_source=?,category_id=NULL WHERE id=? AND user_id=? AND type='income' "
                    "AND bill_rule_id IS NULL",
                    (*values, tx_id, uid),
                )
                if cur.rowcount == 0:
                    raise HTTPException(404, "Доход не найден")
                con.execute("DELETE FROM piggy_bank_movements WHERE user_id=? AND income_transaction_id=?",
                            (uid, tx_id))
            if payload.destination == "piggy":
                con.execute(
                    "INSERT INTO piggy_bank_movements"
                    "(user_id,direction,amount,movement_date,note,source,income_transaction_id) "
                    "VALUES(?, 'deposit', ?, ?, ?, 'external', ?)",
                    (uid, payload.amount, payload.tx_date.isoformat(),
                     payload.income_source or payload.note, tx_id),
                )
            check_piggy_history(con, uid)
            engine.check_reserve(con, uid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return int(tx_id)


def _income_result(uid: int, tx_id: int) -> dict:
    row = next((r for r in engine.incomes(uid) if r["id"] == tx_id), None)
    if row is None:
        raise HTTPException(404, "Доход не найден")
    split = row["split"] or {"reserve": 0, "bills": 0, "free": 0, "piggy": 0}
    as_split = Split(cents(split["reserve"]), cents(split["bills"]), cents(split["free"]), cents(split["piggy"]))
    row["summary"] = engine.summary_text(
        cents(row["amount"]), as_split, float(row["reserve_percent"] or 0), row["income_destination"])
    return row


@router.post("/irregular/incomes")
def create_income(payload: IncomeIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    return _income_result(uid, _write_income(uid, payload))


@router.put("/irregular/incomes/{tx_id}")
def update_income(tx_id: int, payload: IncomeIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    return _income_result(uid, _write_income(uid, payload, tx_id))


@router.get("/irregular/reserve")
def get_reserve(user: TelegramUser = Depends(current_user)) -> dict:
    return engine.reserve_snapshot(_ready(user))


def _insert_move(uid: int, direction: str, payload_amount: float, day: date, reason: str, kind: str,
                 expense: dict | None = None) -> None:
    try:
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            expense_id = None
            if expense is not None:
                expense_id = con.execute(
                    "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note) "
                    "VALUES(?, 'expense', ?, ?, ?, ?)",
                    (uid, payload_amount, day.isoformat(), expense["category_id"], expense["note"]),
                ).lastrowid
            con.execute(
                "INSERT INTO emergency_reserve_movements"
                "(user_id,direction,amount,movement_date,reason,kind,expense_transaction_id) VALUES(?,?,?,?,?,?,?)",
                (uid, direction, payload_amount, day.isoformat(), reason, kind, expense_id),
            )
            engine.check_reserve(con, uid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/irregular/reserve/withdraw")
def withdraw_reserve(payload: ReserveWithdrawIn, user: TelegramUser = Depends(current_user)) -> dict:
    """Take money from the reserve: pay an expense, add it to free money or
    cover today's overspend."""
    uid = _ready(user)
    settings = _configured(uid)
    _check_day(settings, payload.movement_date, "операции")
    expense = None
    if payload.purpose in ("to_free", "cover_overspend") and payload.movement_date != clock.today():
        raise HTTPException(422, "Перевести из резерва можно только сегодняшним днём")
    if payload.purpose == "cover_overspend":
        flow = engine.snapshot(uid)
        if cents(payload.amount) > cents(flow.get("overspend") or 0):
            raise HTTPException(422, "Сумма больше сегодняшнего перерасхода")
    if payload.purpose == "pay_expense":
        if payload.category_id is None:
            raise HTTPException(422, "Выберите категорию расхода")
        with connect() as con:
            if not con.execute("SELECT 1 FROM categories WHERE id=? AND user_id=?",
                               (payload.category_id, uid)).fetchone():
                raise HTTPException(422, "Категория не найдена")
        expense = {"category_id": payload.category_id, "note": payload.reason}
    _insert_move(uid, "withdraw", payload.amount, payload.movement_date, payload.reason,
                 payload.purpose, expense)
    return engine.reserve_snapshot(uid)


@router.post("/irregular/reserve/deposit")
def deposit_reserve(payload: ReserveDepositIn, user: TelegramUser = Depends(current_user)) -> dict:
    """Top up the reserve with outside money or return free money to it."""
    uid = _ready(user)
    settings = _configured(uid)
    _check_day(settings, payload.movement_date, "операции")
    reason = payload.reason
    if payload.kind == "from_free":
        if payload.movement_date != clock.today():
            raise HTTPException(422, "Вернуть в резерв можно только сегодняшним днём")
        flow = engine.snapshot(uid)
        if cents(payload.amount) > max(0, cents(flow.get("free_balance") or 0)):
            raise HTTPException(422, "Нельзя вернуть больше свободных денег")
        reason = reason or "Возврат из свободных денег"
    _insert_move(uid, "deposit", payload.amount, payload.movement_date, reason, payload.kind)
    return engine.reserve_snapshot(uid)


@router.delete("/irregular/reserve/movements/{movement_id}")
def delete_reserve_movement(movement_id: int, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    try:
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT expense_transaction_id FROM emergency_reserve_movements WHERE id=? AND user_id=?",
                (movement_id, uid),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Операция резерва не найдена")
            if row["expense_transaction_id"] is not None:
                raise HTTPException(422, "Это оплата расхода из резерва: удалите сам расход")
            con.execute("DELETE FROM emergency_reserve_movements WHERE id=? AND user_id=?", (movement_id, uid))
            engine.check_reserve(con, uid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return engine.reserve_snapshot(uid)
