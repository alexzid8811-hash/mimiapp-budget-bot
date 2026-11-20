"""HTTP API for the card / buffer / piggy-bank model.

All numbers come from :mod:`app.engine`; this module only validates requests
and stores operations.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Literal
from uuid import uuid4

from fastapi import Depends, HTTPException
from pydantic import Field

from . import payroll_app as base
from . import clock, engine, irregular_engine, payday_delays, piggy_goals, vacation_funds
from .validation import APIModel
from .savings import check_piggy_history
from .auth import TelegramUser, current_user
from .money import amount, cents


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
    # A goal of the piggy bank: the deposit is set aside for it, the
    # withdrawal is taken from it.  None: no goal.
    goal_id: int | None = None


class PiggyToCardIn(APIModel):
    amount: float = Field(gt=0)
    movement_date: date = Field(default_factory=lambda: clock.today())
    note: str = Field(default="", max_length=160)
    # "today" adds the whole amount to today's sum (e.g. a purchase paid from
    # the piggy bank); "cover_overspend" does the same but only up to today's
    # overspend.  In both cases the remaining days keep their limit.
    # "transfer" adds money to the period and is spread over the remaining days.
    purpose: Literal["transfer", "today", "cover_overspend"] = "transfer"
    # The goal the money is taken from (None: the money set aside for no goal).
    goal_id: int | None = None


class DailyCapIn(APIModel):
    # None: no maximum.
    amount: float | None = Field(default=None, gt=0)
    # Where the money above the maximum goes.
    excess_to: Literal["buffer", "piggy"] = "buffer"


class VacationTransferIn(APIModel):
    part: str = Field(pattern=r"^\d{4}-\d{2}:(advance|salary)$")


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


class PaydayDelayIn(APIModel):
    days: int = Field(ge=0, le=366)
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9-]+$")


class PaydayDelayEditIn(APIModel):
    future_days: int | None = Field(ge=0)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


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


def piggy_bank_snapshot(user_id: int, limit: int = 80, since: date | None = None) -> dict:
    """The balance, the goals and the latest ``limit`` movements.  ``since``
    adds every movement from that day on, like /api/transactions: «Период»
    lists the card ↔ piggy transfers of earlier periods too."""
    limit = min(max(limit, 1), 300)
    if since is None:
        where, params = "ORDER BY movement_date DESC,id DESC LIMIT ?", (limit,)
    else:
        where = ("AND (movement_date>=? OR id IN (SELECT id FROM piggy_bank_movements WHERE user_id=? "
                 "ORDER BY movement_date DESC,id DESC LIMIT ?)) ORDER BY movement_date DESC,id DESC")
        params = (since.isoformat(), user_id, limit)
    movements = legacy.rows(
        "SELECT id,direction,amount,movement_date,note,source,purpose,bill_payment_id,"
        "income_transaction_id,expense_transaction_id,created_at,"
        "(SELECT g.title FROM piggy_goal_moves m JOIN piggy_goals g ON g.id=m.goal_id "
        "WHERE m.piggy_movement_id=piggy_bank_movements.id) AS goal_title FROM piggy_bank_movements "
        f"WHERE user_id=? {where}",
        (user_id, *params),
    )
    with legacy.connect() as con:
        goals = piggy_goals.summary(con, user_id)
        vacation_reserves = [row for row in vacation_funds.rows(con, user_id, clock.today()) if not row["transfer_date"]]
    return {"balance": piggy_bank_balance(user_id), "movements": movements,
            "vacation_reserves": vacation_reserves, **goals}


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
    "capital_shortfall", "shortfall_date", "floor_gap", "floor_gap_date", "buffer_floor", "reason",
    "periods", "past_periods", "card_balance", "tomorrow_limit",
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
        (row for row in snapshot["periods"] + snapshot["past_periods"]
         if row["override_key"] == period_start.isoformat() and row["received_editable"]),
        None,
    )


def _delay_movement(con, uid, direction, value, today, *, edited=False):
    from . import accounts_engine

    if not value:
        return
    if direction == "withdraw":
        purpose, title = "payday_delay", "Траты до выплаты"
    elif edited:
        purpose, title = "payday_delay_edit_return", "Возврат за убранные дни задержки"
    else:
        purpose, title = "payday_delay_return", "Возврат остатка после задержки выплаты"
    day = today.isoformat()
    movement = con.execute(
        "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source,purpose) "
        "VALUES(?,?,?,?,?,'daily_budget',?)", (uid, direction, amount(value), day, title, purpose),
    ).lastrowid
    accounts_engine.link_piggy(con, uid, movement, direction=direction, source="daily_budget",
                                value=amount(value), day=day)


def _repay_delay(con, uid, payday, delay, received_amount):
    from . import accounts_engine

    value = min(payday_delays.amount_to_repay(delay), cents(received_amount))
    if value == delay["repaid_cents"]:
        return
    protected = vacation_funds.protected_cents(con, uid)
    movement_id = delay["repayment_movement_id"]
    if not value:
        con.execute("DELETE FROM piggy_bank_movements WHERE id=? AND user_id=?", (movement_id, uid))
    elif movement_id:
        con.execute("UPDATE piggy_bank_movements SET amount=? WHERE id=? AND user_id=?",
                    (amount(value), movement_id, uid))
        con.execute("UPDATE account_transfers SET amount=? WHERE piggy_movement_id=? AND user_id=?",
                    (amount(value), movement_id, uid))
    else:
        day = delay["received_on"].isoformat()
        movement_id = con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source,purpose) "
            "VALUES(?,'deposit',?,?,'Возврат денег за дни задержки','external','payday_delay_repayment')",
            (uid, amount(value), day),
        ).lastrowid
        con.execute("UPDATE payday_delays SET repayment_movement_id=? WHERE user_id=? AND payday=?",
                    (movement_id, uid, payday.isoformat()))
        if accounts_engine.settings_for(con, uid)["enabled"]:
            places, _ = accounts_engine.placement(con, uid)
            accounts_engine._linked(con, uid, places.get("buffer"), places.get("piggy"), amount(value), day,
                                    piggy=movement_id)
    try:
        check_piggy_history(con, uid, protected)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/cashflow/payday-delays/{period_start}")
def extend_payday_delay(
    period_start: date, payload: PaydayDelayIn, user: TelegramUser = Depends(current_user),
) -> dict:
    uid = legacy.user_ready(user)
    if irregular_engine.is_irregular(uid) or not cashflow_settings(uid)["cashflow_enabled"]:
        raise HTTPException(422, "Задержка выплаты доступна в режиме зарплаты с включённым буфером")
    today = clock.today()
    payday = period_start - timedelta(days=1)
    row = editable_payday_row(uid, period_start)
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        delays = payday_delays.load(con, uid)
        delay = delays.get(payday)
        if delay and payload.request_id in delay["extensions"]:
            return {"ok": True}
        if (not row or not row["income_pending"] or row["payday"] != payday.isoformat()
                or (delay and delay["received_on"] is not None)
                or con.execute(
                    "SELECT 1 FROM cashflow_income_overrides WHERE user_id=? AND confirmed=1 "
                    "AND period_start IN (?,?)", (uid, period_start.isoformat(), payday.isoformat()),
                ).fetchone()):
            raise HTTPException(422, "Задержку можно добавить к выплате со статусом «ждём»")
        daily = cents(delay["daily"] if delay else row["delay_daily"])
        covered = payday_delays.bridge_days(delay) if delay else []
        first_day = max(today, period_start, covered[-1] + timedelta(days=1) if covered else today)
        all_covered = {
            day for other in delays.values() for day in payday_delays.covered_days(other)
        }
        days = [first_day + timedelta(days=offset) for offset in range(payload.days)]
        if any(day in all_covered for day in days):
            raise HTTPException(422, "На эти дни уже выделены деньги по другой задержанной выплате")
        total = daily * len(days)
        if total:
            piggy_goals.check_withdrawal(con, uid, amount(total), None)
        extensions = dict(delay["extensions"]) if delay else {}
        extensions[payload.request_id] = [day.isoformat() for day in days]
        con.execute(
            "INSERT INTO payday_delays(user_id,payday,daily,extensions) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,payday) DO UPDATE SET extensions=excluded.extensions",
            (uid, payday.isoformat(), amount(daily), json.dumps(extensions)),
        )
        con.execute("DELETE FROM card_allocations WHERE user_id=? AND period_start=?", (uid, period_start.isoformat()))
        _delay_movement(con, uid, "withdraw", total, today)
        try:
            check_piggy_history(con, uid)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"ok": True}


@app.put("/api/cashflow/payday-delays/{period_start}")
def edit_payday_delay(
    period_start: date, payload: PaydayDelayEditIn, user: TelegramUser = Depends(current_user),
) -> dict:
    uid = legacy.user_ready(user)
    if irregular_engine.is_irregular(uid) or not cashflow_settings(uid)["cashflow_enabled"]:
        raise HTTPException(422, "Задержка выплаты доступна в режиме зарплаты с включённым буфером")
    payday = period_start - timedelta(days=1)
    row = editable_payday_row(uid, period_start)
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        today = clock.today()
        delays = payday_delays.load(con, uid)
        delay = delays.get(payday)
        if (not row or not row["delay_pending"] or not delay or delay["received_on"] is not None
                or row["delay_revision"] != payload.revision
                or payday_delays.revision(delay, today) != payload.revision):
            raise HTTPException(409, "Задержка изменилась. Обновите буфер и откройте её снова.")

        covered = payday_delays.bridge_days(delay)
        future = [day for day in covered if day > today]
        cancel = payload.future_days is None
        if cancel and not row["delay_can_cancel"]:
            raise HTTPException(422, "Можно изменить будущие дни. Для расчёта с копилкой подтвердите выплату после получения.")
        requested = 0 if payload.future_days is None else payload.future_days
        change = requested - len(future)
        if change > 366:
            raise HTTPException(422, "За одно изменение можно добавить не больше 366 дней")
        value = cents(delay["daily"]) * abs(change)
        extensions = delay["extensions"]
        if change < 0:
            removed = {day.isoformat() for day in future[requested:]}
            extensions = {key: [day for day in days if day not in removed] for key, days in extensions.items()}
        elif change > 0:
            first_day = max(today, payday, *covered) + timedelta(days=1)
            added = [first_day + timedelta(days=index) for index in range(change)]
            if set(added).intersection(day for key, other in delays.items() if key != payday
                                       for day in payday_delays.covered_days(other)):
                raise HTTPException(422, "На эти дни уже выделены деньги по другой задержанной выплате")
            if value:
                piggy_goals.check_withdrawal(con, uid, amount(value), None)
            extensions[str(uuid4())] = [day.isoformat() for day in added]
        if cancel:
            con.execute("DELETE FROM payday_delays WHERE user_id=? AND payday=?", (uid, payday.isoformat()))
        elif change:
            con.execute("UPDATE payday_delays SET extensions=? WHERE user_id=? AND payday=?",
                        (json.dumps(extensions), uid, payday.isoformat()))
        _delay_movement(con, uid, "withdraw" if change > 0 else "deposit", value, today, edited=True)
        try:
            check_piggy_history(con, uid)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"ok": True}


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
    if not row["delay_pending"] and not row["received_on"]:
        engine.freeze_allocations_before(uid, period_start)
    # Entering the amount of a payday that has already come (even through
    # «Изменить») means the money arrived: it confirms the payday.
    confirmed = payload.confirmed or row["income_pending"]
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        today = clock.today()
        payday = date.fromisoformat(row["payday"])
        delay = payday_delays.load(con, uid).get(payday)
        if delay and delay["received_on"] is None and confirmed:
            delay["received_on"] = today
            delay["funding_on"] = payday_delays.first_regular_day(delay, today)
            unused_days = len(payday_delays.bridge_days(delay)) - len(payday_delays.covered_days(delay))
            returned = cents(delay["daily"]) * unused_days
            _delay_movement(con, uid, "deposit", returned, today)
            con.execute("UPDATE payday_delays SET received_on=?,funding_on=? WHERE user_id=? AND payday=?",
                        (today.isoformat(), delay["funding_on"].isoformat(), uid, payday.isoformat()))
        if delay and delay["funding_on"]:
            _repay_delay(con, uid, payday, delay, payload.amount)
        if delay and (confirmed or delay["received_on"]):
            con.execute("DELETE FROM card_allocations WHERE user_id=? AND period_start=?",
                        (uid, period_start.isoformat()))
        received_on = (delay["received_on"] or today).isoformat() if delay else row["payday"]
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
            con, uid, period_start=period_start.isoformat(), payday=received_on, value=payload.amount,
            confirmed=bool(confirmed or (was_confirmed and was_confirmed["confirmed"])),
            account_id=payload.account_id,
        )
    return get_buffer(user)


@app.delete("/api/cashflow/income-overrides/{period_start}")
def delete_cashflow_income_override(
    period_start: date, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        if period_start - timedelta(days=1) in payday_delays.load(con, uid):
            raise HTTPException(422, "Для задержанной выплаты можно изменить фактическую сумму")
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
def get_piggy_bank(since: date | None = None, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    return piggy_bank_snapshot(uid, since=since)


def _insert_piggy(user_id: int, direction: str, amount: float, day: date, note: str,
                  source: str, purpose: str | None = None, *, account_id: int | None = None,
                  account_move: bool = True, goal_id: int | None = None) -> dict:
    from . import accounts_engine
    from .accounts_app import check_account

    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        check_account(con, user_id, account_id, operation="piggy")
        if direction == "withdraw":
            # Money set aside for a goal leaves only from that goal.
            piggy_goals.check_withdrawal(con, user_id, amount, goal_id)
        movement_id = con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source,purpose) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, direction, amount, day.isoformat(), note, source, purpose),
        ).lastrowid
        piggy_goals.link_movement(con, user_id, movement_id, goal_id, direction, amount, day.isoformat())
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
                         account_move=payload.account_move, goal_id=payload.goal_id)


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
                             "daily_budget", payload.purpose, goal_id=payload.goal_id)
    return {"movement": movement, **piggy_bank_snapshot(uid)}


@app.delete("/api/piggy-bank/movements/{movement_id}")
def delete_piggy_bank_movement(
    movement_id: int, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        movement = con.execute(
            "SELECT income_transaction_id,bill_payment_id,expense_transaction_id,purpose,movement_date "
            "FROM piggy_bank_movements "
            "WHERE id=? AND user_id=?",
            (movement_id, uid),
        ).fetchone()
        if movement is None:
            raise HTTPException(404, "Операция копилки не найдена")
        if movement["purpose"] in ("payday_delay", "payday_delay_return", "payday_delay_edit_return",
                                   "payday_delay_repayment"):
            raise HTTPException(422, "Эта операция связана с задержкой выплаты")
        if movement["purpose"] in ("vacation_deposit", "vacation_existing_deposit"):
            raise HTTPException(422, "Это отпускные. Пополнение меняется через запись отпуска в настройках")
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
        protected = vacation_funds.protected_cents(con, uid)
        if movement["purpose"] == "vacation_buffer":
            if movement["movement_date"] != clock.today().isoformat():
                raise HTTPException(422, "Перевод отпускных можно отменить в день перевода")
            con.execute("DELETE FROM card_allocations WHERE user_id=? AND funded_on=?",
                        (uid, movement["movement_date"]))
        con.execute("DELETE FROM piggy_bank_movements WHERE id=? AND user_id=?", (movement_id, uid))
        try:
            check_piggy_history(con, uid, protected)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # A deposit for a goal takes its goal money with it; a deposit for no
        # goal may not leave the goals above the balance.
        piggy_goals.check_fits(con, uid)
    return {"ok": True, **piggy_bank_snapshot(uid)}


@app.post("/api/piggy-bank/vacations/{vacation_id}/transfer")
def transfer_vacation_reserve(
    vacation_id: int, payload: VacationTransferIn, user: TelegramUser = Depends(current_user),
) -> dict:
    """Release this payout's share into the buffer exactly once."""
    from . import accounts_engine

    uid = legacy.user_ready(user)
    today = clock.today()
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        already_transferred = con.execute(
            "SELECT 1 FROM piggy_bank_movements WHERE user_id=? AND vacation_id=? AND vacation_part=?",
            (uid, vacation_id, payload.part),
        ).fetchone()
        if not already_transferred:
            settings = con.execute("SELECT budget_mode,cashflow_enabled FROM settings WHERE user_id=?", (uid,)).fetchone()
            if settings["budget_mode"] != "payroll" or not settings["cashflow_enabled"]:
                raise HTTPException(422, "Перевод доступен в режиме «Зарплата и аванс» с включённым буфером")
            row = next((item for item in vacation_funds.rows(con, uid, today)
                        if item["vacation_id"] == vacation_id and item["part"] == payload.part), None)
            if row is None:
                raise HTTPException(404, "Резерв отпускных не найден")
            if not row["can_transfer"]:
                raise HTTPException(422, "Перевод доступен с даты выплаты, когда вся сумма есть в копилке")
            movement_id = con.execute(
                "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source,purpose,"
                "vacation_id,vacation_part) VALUES(?,'withdraw',?,?,'Отпускные','external','vacation_buffer',?,?)",
                (uid, row["amount"], today.isoformat(), vacation_id, payload.part),
            ).lastrowid
            try:
                check_piggy_history(con, uid)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            if accounts_engine.settings_for(con, uid)["enabled"]:
                places, _ = accounts_engine.placement(con, uid)
                accounts_engine._linked(con, uid, places.get("piggy"), places.get("buffer"),
                                        row["amount"], today.isoformat(), piggy=movement_id)
            # The transfer funds today's new period even if its salary was confirmed first.
            con.execute("DELETE FROM card_allocations WHERE user_id=? AND funded_on=?", (uid, today.isoformat()))
    return get_buffer(user)


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

app.include_router(piggy_goals.router)
