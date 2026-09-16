from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import Field

from . import payroll_app as base
from . import clock, planning
from .validation import APIModel
from .savings import check_piggy_history, piggy_effect
from .auth import TelegramUser, current_user
from .budget import add_months
from .cashflow import calculate_cashflow_plan


app = base.app
legacy = base.legacy


class CashflowSettingsIn(APIModel):
    cashflow_enabled: bool = False
    start_date: date
    start_capital: float = Field(default=0, ge=0)


class PiggyBankMovementIn(APIModel):
    amount: float = Field(gt=0)
    movement_date: date = Field(default_factory=clock.today)
    note: str = Field(default="", max_length=160)


def cashflow_settings(user_id: int) -> dict:
    data = legacy.one(
        "SELECT cashflow_enabled,cashflow_start_date,initial_reserve,forecast_months "
        "FROM settings WHERE user_id=?",
        (user_id,),
    ) or {}
    return {
        "cashflow_enabled": int(data.get("cashflow_enabled") or 0),
        "start_date": data.get("cashflow_start_date") or clock.today().isoformat(),
        "start_capital": float(data.get("initial_reserve") or 0),
        "forecast_months": int(data.get("forecast_months") or 4),
    }


def _add(target: dict[date, float], day: date, amount: float) -> None:
    target[day] = round(target.get(day, 0.0) + float(amount), 2)


def planned_income_map(user_id: int, start: date, end: date) -> dict[date, float]:
    return planning.income_map(user_id, start, end)


def planned_mandatory_map(user_id: int, start: date, end: date) -> dict[date, float]:
    return planning.mandatory_map(user_id, start, end)



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


def piggy_bank_effect(user_id: int, start: date, end: date) -> float:
    return piggy_effect(user_id, start, end)


def piggy_bank_snapshot(user_id: int, limit: int = 80) -> dict:
    movements = legacy.rows(
        "SELECT id,direction,amount,movement_date,note,created_at FROM piggy_bank_movements "
        "WHERE user_id=? ORDER BY movement_date DESC,id DESC LIMIT ?",
        (user_id, min(max(limit, 1), 300)),
    )
    return {"balance": piggy_bank_balance(user_id), "movements": movements}


def payday_boundaries(user_id: int, start: date, end: date) -> list[dict]:
    return planning.payday_boundaries(user_id, start, end)


def cashflow_period_rows(
    user_id: int,
    *,
    today: date,
    horizon_end: date,
    opening_balance_before_today_spend: float,
    daily_target: float,
    spent_today: float = 0,
) -> list[dict]:
    boundaries = payday_boundaries(user_id, today + timedelta(days=1), horizon_end)
    starts = [{"date": today, "kind": "сейчас"}, *boundaries]
    income = planned_income_map(user_id, today + timedelta(days=1), horizon_end)
    mandatory = planned_mandatory_map(user_id, today + timedelta(days=1), horizon_end)
    buffer_before = 0.0
    result: list[dict] = []

    for index, item in enumerate(starts):
        period_start = item["date"]
        period_end = (
            starts[index + 1]["date"] - timedelta(days=1)
            if index + 1 < len(starts)
            else horizon_end
        )
        if period_start > horizon_end:
            break
        days = (period_end - period_start).days + 1
        planned_income = round(
            sum(amount for day, amount in income.items() if period_start <= day <= period_end), 2
        )
        received = round(
            (opening_balance_before_today_spend if index == 0 else 0.0) + planned_income, 2
        )
        bills = round(
            sum(amount for day, amount in mandatory.items() if period_start <= day <= period_end), 2
        )
        free = round(received - bills, 2)
        planned_spending = round(daily_target * days + (max(0, spent_today - daily_target) if index == 0 else 0), 2)
        raw_after = round(buffer_before + free - planned_spending, 2)
        buffer_after = round(max(0.0, raw_after), 2)
        put_aside = round(max(0.0, buffer_after - buffer_before), 2)
        take = round(max(0.0, buffer_before - buffer_after), 2)
        result.append(
            {
                "start": period_start.isoformat(),
                "end": period_end.isoformat(),
                "kind": item["kind"],
                "received": received,
                "days": days,
                "mandatory": bills,
                "free": free,
                "daily": round(daily_target, 2),
                "put_aside": put_aside,
                "take": take,
                "buffer": buffer_after,
                "shortfall": round(max(0.0, -raw_after), 2),
            }
        )
        buffer_before = raw_after
    return result


def cashflow_snapshot(user_id: int) -> dict:
    settings = cashflow_settings(user_id)
    today = clock.today()
    if not settings["cashflow_enabled"]:
        return {"enabled": False, "settings": settings}

    start_date = min(date.fromisoformat(settings["start_date"]), today)
    start_capital = float(settings["start_capital"])
    months = max(1, min(12, int(settings["forecast_months"])))
    horizon_end = add_months(today, months)

    # Past scheduled income is only a forecast until the user records that it
    # was actually received. Otherwise an unpaid salary/vacation payment would
    # silently inflate the real balance carried from the starting capital.
    actual_income_history = legacy.actual_income(user_id, start_date, today)
    mandatory_history = planned_mandatory_map(user_id, start_date, today)
    spent_history = legacy.discretionary_spent(user_id, start_date, today)

    piggy_effect = piggy_bank_effect(user_id, start_date, today)
    current_cash = round(
        start_capital + actual_income_history - sum(mandatory_history.values()) - spent_history - piggy_effect,
        2,
    )
    spent_today = legacy.discretionary_spent(user_id, today, today)
    opening_before_today_spend = round(current_cash + spent_today, 2)

    tomorrow = today + timedelta(days=1)
    income_future = planned_income_map(user_id, tomorrow, horizon_end) if tomorrow <= horizon_end else {}
    mandatory_future = planned_mandatory_map(user_id, tomorrow, horizon_end) if tomorrow <= horizon_end else {}

    plan = calculate_cashflow_plan(
        today=today,
        horizon_end=horizon_end,
        opening_balance_before_today_spend=opening_before_today_spend,
        spent_today=spent_today,
        income_by_date=income_future,
        mandatory_by_date=mandatory_future,
    )

    period = planning.user_period(user_id, today)
    spent_period = legacy.discretionary_spent(user_id, max(period.start, start_date), today)
    days_left = max(1, (period.end - today).days + 1)
    remaining_period = round(plan.available_today + plan.daily_target * max(0, days_left - 1), 2)
    period_budget = round(spent_period + remaining_period, 2)
    next_income = next((row for row in plan.timeline if row["income"] > 0), None)

    if plan.capital_shortfall > 0:
        reason = (
            f"Даже без повседневных трат не хватает {plan.capital_shortfall:.2f}. "
            "Нужно увеличить стартовый капитал или уменьшить обязательные платежи."
        )
    else:
        reason = (
            "Всё сверх безопасного дневного лимита остаётся в виртуальном буфере. "
            "Он автоматически покрывает будущие обязательные платежи и слабые выплаты."
        )

    periods = cashflow_period_rows(
        user_id,
        today=today,
        horizon_end=horizon_end,
        opening_balance_before_today_spend=opening_before_today_spend,
        daily_target=plan.daily_target,
        spent_today=spent_today,
    )

    return {
        "enabled": True,
        "settings": settings,
        "today": today.isoformat(),
        "horizon_end": horizon_end.isoformat(),
        "current_cash": current_cash,
        "piggy_bank_balance": piggy_bank_balance(user_id),
        "daily_target": plan.daily_target,
        "available_today": plan.available_today,
        "buffer_balance": plan.buffer_balance,
        "capital_shortfall": plan.capital_shortfall,
        "period_budget": period_budget,
        "remaining_period": remaining_period,
        "spent_period": spent_period,
        "projected_end_balance": plan.projected_end_balance,
        "minimum_projected_balance": plan.minimum_projected_balance,
        "next_income": next_income,
        "reason": reason,
        "timeline": plan.timeline[:24],
        "periods": periods,
    }


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
            "UPDATE settings SET cashflow_enabled=?,cashflow_start_date=?,initial_reserve=?,updated_at=CURRENT_TIMESTAMP "
            "WHERE user_id=?",
            (int(payload.cashflow_enabled), payload.start_date.isoformat(), payload.start_capital, uid),
        )
        con.execute("DELETE FROM reserve_movements WHERE user_id=? AND source='auto'", (uid,))
    return cashflow_settings(uid)


@app.get("/api/cashflow")
def get_cashflow(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    return cashflow_snapshot(uid)


@app.get("/api/buffer")
def get_buffer(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    snapshot = cashflow_snapshot(uid)
    if not snapshot.get("enabled"):
        return {"enabled": False, "settings": snapshot["settings"], "periods": []}
    return {
        "enabled": True,
        "today": snapshot["today"],
        "horizon_end": snapshot["horizon_end"],
        "daily_target": snapshot["daily_target"],
        "available_today": snapshot["available_today"],
        "buffer_balance": snapshot["buffer_balance"],
        "capital_shortfall": snapshot["capital_shortfall"],
        "periods": snapshot["periods"],
    }


@app.get("/api/piggy-bank")
def get_piggy_bank(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    return piggy_bank_snapshot(uid)


def add_piggy_bank_movement(
    user_id: int, direction: Literal["deposit", "withdraw"], payload: PiggyBankMovementIn
) -> dict:
    if payload.movement_date > clock.today():
        raise HTTPException(422, "Дата операции не может быть в будущем")
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        movement_id = con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note) VALUES(?,?,?,?,?)",
            (user_id, direction, payload.amount, payload.movement_date.isoformat(), payload.note),
        ).lastrowid
        try:
            check_piggy_history(con, user_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    legacy.invalidate_current_auto_reserve(user_id)
    return legacy.one(
        "SELECT id,direction,amount,movement_date,note,created_at "
        "FROM piggy_bank_movements WHERE id=? AND user_id=?",
        (movement_id, user_id),
    ) or {}


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
    legacy.invalidate_current_auto_reserve(uid)
    return {"ok": True, **piggy_bank_snapshot(uid)}


from .backup import router as backup_router

app.include_router(backup_router)
