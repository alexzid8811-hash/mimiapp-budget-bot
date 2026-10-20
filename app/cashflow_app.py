"""HTTP API for the card / buffer / piggy-bank model.

All numbers come from :mod:`app.engine`; this module only validates requests
and stores operations.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import Field

from . import payroll_app as base
from . import clock, engine, irregular_engine
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
    # Cards and accounts, outside money only: the card it came from (a
    # deposit) or went to (a withdrawal); None: from or to outside.
    account_id: int | None = None
    # False: the money already lies on the piggy bank's card («Добавить в копилку»).
    account_move: bool = True


class PiggyToCardIn(APIModel):
    amount: float = Field(gt=0)
    movement_date: date = Field(default_factory=lambda: clock.today())
    note: str = Field(default="", max_length=160)
    # "today" adds the whole amount to today's sum (e.g. a purchase paid from
    # the piggy bank); "cover_overspend" does the same but only up to today's
    # overspend.  In both cases the remaining days keep their limit.
    # "transfer" adds money to the period and is spread over the remaining days.
    purpose: Literal["transfer", "today", "cover_overspend"] = "transfer"


class DailyCapIn(APIModel):
    # None: no maximum.
    amount: float | None = Field(default=None, gt=0)
    # Where the money above the maximum goes.
    excess_to: Literal["buffer", "piggy"] = "buffer"


class BufferFloorIn(APIModel):
    # None or 0: no minimum.
    amount: float | None = Field(default=None, ge=0)


class BufferToPiggyIn(APIModel):
    amount: float = Field(gt=0)
    note: str = Field(default="", max_length=160)


class CashflowIncomeOverrideIn(APIModel):
    amount: float = Field(ge=0)
    # True when the user confirms the payout actually arrived.
    confirmed: bool = False
    # Cards and accounts: where the confirmed money arrived (main by default).
    account_id: int | None = None


def today_numbers(user_id: int) -> dict:
    """Today's available money and overspend of the active budget mode."""
    if irregular_engine.is_irregular(user_id):
        return irregular_engine.snapshot(user_id)
    return engine.compute(user_id, force_enabled=bool(cashflow_settings(user_id)["cashflow_enabled"]))


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
        "SELECT id,direction,amount,movement_date,note,source,purpose,bill_payment_id,"
        "income_transaction_id,expense_transaction_id,created_at FROM piggy_bank_movements "
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


@app.put("/api/daily-cap")
def save_daily_cap(payload: DailyCapIn, user: TelegramUser = Depends(current_user)) -> dict:
    """«Максимум в день» from today on: the current period is recalculated
    for its remaining days and every later period from scratch."""
    uid = legacy.user_ready(user)
    if irregular_engine.is_irregular(uid):
        raise HTTPException(422, "Максимум в день работает в режиме «Зарплата и аванс»")
    engine.set_daily_cap(uid, payload.amount, to_piggy=payload.excess_to == "piggy")
    return cashflow_settings(uid)


@app.put("/api/buffer-floor")
def save_buffer_floor(payload: BufferFloorIn, user: TelegramUser = Depends(current_user)) -> dict:
    """«Минимум буфера»: later card periods get only what keeps the buffer
    at or above it."""
    uid = legacy.user_ready(user)
    if irregular_engine.is_irregular(uid):
        raise HTTPException(422, "Минимум буфера работает в режиме «Зарплата и аванс»")
    engine.set_buffer_floor(uid, payload.amount)
    return cashflow_settings(uid)


@app.post("/api/buffer/to-piggy")
def buffer_to_piggy(payload: BufferToPiggyIn, user: TelegramUser = Depends(current_user)) -> dict:
    """Spare buffer money (kept by the daily maximum) to the piggy bank.  Only
    as much as leaves every card period and obligation as it was."""
    uid = legacy.user_ready(user)
    snapshot = cashflow_snapshot(uid)
    if not snapshot.get("enabled") or irregular_engine.is_irregular(uid):
        raise HTTPException(422, "Буфер выключен")
    spare = cents(snapshot.get("spare_buffer") or 0)
    if cents(payload.amount) > spare:
        raise HTTPException(
            422, f"Без ущерба для буфера можно перевести не больше {spare / 100:.2f}".replace(".", ","),
        )
    from . import accounts_engine

    day = clock.today().isoformat()
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        movement_id = con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source,purpose) "
            "VALUES(?,'deposit',?,?,?,'external','from_buffer')",
            (uid, payload.amount, day, payload.note or "Лишнее из буфера"),
        ).lastrowid
        # Cards and accounts: from the buffer's card to the piggy bank's.
        if accounts_engine.settings_for(con, uid)["enabled"]:
            places, _ = accounts_engine.placement(con, uid)
            accounts_engine._linked(con, uid, places.get("buffer"), places.get("piggy"), payload.amount, day,
                                    piggy=movement_id)
    return {**piggy_bank_snapshot(uid), "buffer": get_buffer(user)}


@app.get("/api/cashflow")
def get_cashflow(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    return cashflow_snapshot(uid)


BUFFER_KEYS = (
    "today", "horizon_end", "daily_target", "available_today", "buffer_balance",
    "capital_shortfall", "shortfall_date", "periods", "past_periods", "card_balance", "tomorrow_limit",
    "daily_cap", "daily_cap_to", "period_cap_kept", "period_cap_kept_piggy", "spare_buffer",
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
    row = editable_payday_row(uid, period_start)
    if row is None:
        raise HTTPException(422, "Фактическую сумму можно ввести только для текущей или будущей выплаты")
    # Lock in the money already shown for every earlier period before this
    # edit can change the plan, so the edit only ever affects this period and
    # the ones after it, no matter which order payouts are edited in.
    engine.freeze_allocations_before(uid, period_start)
    # Entering the amount of a payday that has already come (even through
    # «Изменить») means the money arrived: it confirms the payday.
    confirmed = payload.confirmed or row["income_pending"]
    with legacy.connect() as con:
        if payload.account_id is not None:
            from .accounts_app import check_account

            check_account(con, uid, payload.account_id, operation="income")
        was_confirmed = con.execute(
            "SELECT confirmed FROM cashflow_income_overrides WHERE user_id=? AND period_start=?",
            (uid, period_start.isoformat()),
        ).fetchone()
        con.execute(
            "INSERT INTO cashflow_income_overrides(user_id,period_start,amount,confirmed) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,period_start) DO UPDATE SET "
            "amount=excluded.amount,confirmed=MAX(confirmed,excluded.confirmed),"
            "updated_at=CURRENT_TIMESTAMP",
            (uid, period_start.isoformat(), payload.amount, int(confirmed)),
        )
        if confirmed and row["income_pending"]:
            # Today's morning report was built without this money: send it
            # again with the new numbers (if it has already gone out).
            sent = con.execute(
                "SELECT 1 FROM morning_reports WHERE user_id=? AND report_date=?",
                (uid, clock.today().isoformat()),
            ).fetchone()
            if sent:
                con.execute("UPDATE settings SET morning_report_resend=1 WHERE user_id=?", (uid,))
        # The received money arrives on an account; an unconfirmed payout
        # never touches a balance.
        from . import accounts_engine

        accounts_engine.sync_payroll_income(
            con, uid, period_start=period_start.isoformat(), payday=row["payday"], value=payload.amount,
            confirmed=bool(confirmed or (was_confirmed and was_confirmed["confirmed"])),
            account_id=payload.account_id,
        )
    return get_buffer(user)


@app.delete("/api/cashflow/income-overrides/{period_start}")
def delete_cashflow_income_override(
    period_start: date, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = legacy.user_ready(user)
    if editable_payday_row(uid, period_start) is None:
        raise HTTPException(422, "Выплата закрытого периода не изменяется")
    # Freeze with the override still in effect: resetting it is itself an
    # edit at this payday, so everything before it stays exactly as shown.
    engine.freeze_allocations_before(uid, period_start)
    with legacy.connect() as con:
        deleted = con.execute(
            "DELETE FROM cashflow_income_overrides WHERE user_id=? AND period_start=?",
            (uid, period_start.isoformat()),
        )
        con.execute(
            "DELETE FROM account_incomes WHERE user_id=? AND payroll_period_start=?",
            (uid, period_start.isoformat()),
        )
    if deleted.rowcount == 0:
        raise HTTPException(404, "Корректировка выплаты не найдена")
    return get_buffer(user)


@app.get("/api/piggy-bank")
def get_piggy_bank(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    return piggy_bank_snapshot(uid)


def _insert_piggy(user_id: int, direction: str, amount: float, day: date, note: str,
                  source: str, purpose: str | None = None, *, account_id: int | None = None,
                  account_move: bool = True) -> dict:
    from . import accounts_engine
    from .accounts_app import check_account

    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        check_account(con, user_id, account_id, operation="piggy")
        movement_id = con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source,purpose) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, direction, amount, day.isoformat(), note, source, purpose),
        ).lastrowid
        # Cards and accounts: the money moves between the envelopes' cards.
        accounts_engine.link_piggy(
            con, user_id, movement_id, direction=direction, source=source, value=amount,
            day=day.isoformat(), account_id=account_id, account_move=account_move,
        )
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
        flow = today_numbers(user_id)
        if cents(payload.amount) > max(0, cents(flow.get("available_today") or 0)):
            raise HTTPException(422, "Нельзя перенести больше неизрасходованного остатка за день")
    return _insert_piggy(user_id, direction, payload.amount, payload.movement_date,
                         payload.note, payload.source, account_id=payload.account_id,
                         account_move=payload.account_move)


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
        flow = today_numbers(uid)
        if cents(payload.amount) > cents(flow.get("overspend") or 0):
            raise HTTPException(422, "Сумма больше сегодняшнего перерасхода")
    note = payload.note or {
        "cover_overspend": "Покрытие перерасхода", "today": "Перевод на сегодня",
    }.get(payload.purpose, "Перевод на карту")
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
        movement = con.execute(
            "SELECT income_transaction_id,bill_payment_id,expense_transaction_id,purpose FROM piggy_bank_movements "
            "WHERE id=? AND user_id=?",
            (movement_id, uid),
        ).fetchone()
        if movement is None:
            raise HTTPException(404, "Операция копилки не найдена")
        # A linked movement is one half of an operation; deleting it alone
        # would leave the income or the bill remainder pointing nowhere.
        if movement["income_transaction_id"] is not None:
            raise HTTPException(422, "Это пополнение создано доходом: измените или удалите сам доход")
        if movement["bill_payment_id"] is not None:
            raise HTTPException(422, "Это остаток обязательного платежа: измените сумму в самом платеже")
        if movement["expense_transaction_id"] is not None:
            raise HTTPException(422, "Это трата из копилки: измените или удалите саму трату")
        if movement["purpose"] == "cap_excess":
            raise HTTPException(422, "Это деньги сверх максимума в день: они меняются вместе с бюджетом")
        if con.execute(
            "SELECT 1 FROM account_transfers WHERE user_id=? AND piggy_movement_id=? AND kind='credit_payment'",
            (uid, movement_id),
        ).fetchone():
            raise HTTPException(422, "Это погашение кредитки: удалите его в истории кредитки")
        con.execute("DELETE FROM piggy_bank_movements WHERE id=? AND user_id=?", (movement_id, uid))
        try:
            check_piggy_history(con, uid)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"ok": True, **piggy_bank_snapshot(uid)}


from .backup import router as backup_router

app.include_router(backup_router)

from .excel_export import router as excel_router

app.include_router(excel_router)

from .analytics import router as analytics_router

app.include_router(analytics_router)

from .irregular_app import router as irregular_router

app.include_router(irregular_router)

from .accounts_app import router as accounts_router

app.include_router(accounts_router)
