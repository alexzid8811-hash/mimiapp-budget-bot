from __future__ import annotations

from datetime import date, timedelta

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from . import payroll_app as base
from .auth import TelegramUser, current_user
from .budget import add_months
from .cashflow import calculate_cashflow_plan


app = base.app
legacy = base.legacy


class CashflowSettingsIn(BaseModel):
    cashflow_enabled: bool = False
    start_date: date
    start_capital: float = Field(default=0, ge=0)


def cashflow_settings(user_id: int) -> dict:
    data = legacy.one(
        "SELECT cashflow_enabled,cashflow_start_date,initial_reserve,forecast_months "
        "FROM settings WHERE user_id=?",
        (user_id,),
    ) or {}
    return {
        "cashflow_enabled": int(data.get("cashflow_enabled") or 0),
        "start_date": data.get("cashflow_start_date") or date.today().isoformat(),
        "start_capital": float(data.get("initial_reserve") or 0),
        "forecast_months": int(data.get("forecast_months") or 4),
    }


def _add(target: dict[date, float], day: date, amount: float) -> None:
    target[day] = round(target.get(day, 0.0) + float(amount), 2)


def planned_income_map(user_id: int, start: date, end: date) -> dict[date, float]:
    result: dict[date, float] = {}
    cfg = base.payroll_settings(user_id)
    vacations = base.vacation_rows(user_id)

    if int(cfg["payroll_enabled"]):
        for event in base.payroll_events_between(start, end, base.payroll_config(user_id), vacations):
            _add(result, event["date"], event["amount"])
        rules = legacy.rows(
            "SELECT amount,day_of_month FROM income_rules WHERE user_id=? AND active=1 AND kind='other'",
            (user_id,),
        )
        for rule in rules:
            for day in legacy.occurrences([int(rule["day_of_month"])], start, end):
                _add(result, day, rule["amount"])
    else:
        rules = legacy.rows(
            "SELECT amount,day_of_month,kind,is_payday FROM income_rules WHERE user_id=? AND active=1",
            (user_id,),
        )
        for rule in rules:
            shifted = bool(rule["is_payday"]) or rule["kind"] in {"salary", "advance"}
            for day in legacy.occurrences(
                [int(rule["day_of_month"])],
                start,
                end,
                move_to_previous_workday=shifted,
            ):
                _add(result, day, rule["amount"])
        for vacation in vacations:
            payment_date = date.fromisoformat(vacation["payment_date"])
            if start <= payment_date <= end:
                _add(result, payment_date, vacation["amount"])

    manual = legacy.rows(
        "SELECT tx_date,amount FROM transactions WHERE user_id=? AND type='income' AND tx_date BETWEEN ? AND ?",
        (user_id, start.isoformat(), end.isoformat()),
    )
    for row in manual:
        _add(result, date.fromisoformat(row["tx_date"]), row["amount"])
    return result


def planned_mandatory_map(user_id: int, start: date, end: date) -> dict[date, float]:
    result: dict[date, float] = {}
    rules = legacy.rows(
        "SELECT amount,day_of_month FROM bill_rules WHERE user_id=? AND active=1",
        (user_id,),
    )
    for rule in rules:
        for day in legacy.occurrences([int(rule["day_of_month"])], start, end):
            _add(result, day, rule["amount"])
    return result


def cashflow_snapshot(user_id: int) -> dict:
    settings = cashflow_settings(user_id)
    today = date.today()
    if not settings["cashflow_enabled"]:
        return {"enabled": False, "settings": settings}

    start_date = min(date.fromisoformat(settings["start_date"]), today)
    start_capital = float(settings["start_capital"])
    months = max(1, min(12, int(settings["forecast_months"])))
    horizon_end = add_months(today, months)

    income_history = planned_income_map(user_id, start_date, today)
    mandatory_history = planned_mandatory_map(user_id, start_date, today)
    spent_history = legacy.discretionary_spent(user_id, start_date, today)

    current_cash = round(
        start_capital + sum(income_history.values()) - sum(mandatory_history.values()) - spent_history,
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

    period = legacy.current_period(today, legacy.payday_days(user_id))
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

    return {
        "enabled": True,
        "settings": settings,
        "today": today.isoformat(),
        "horizon_end": horizon_end.isoformat(),
        "current_cash": current_cash,
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
    }


@app.get("/api/cashflow-settings")
def get_cashflow_settings(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    return cashflow_settings(uid)


@app.put("/api/cashflow-settings")
def save_cashflow_settings(payload: CashflowSettingsIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    if payload.start_date > date.today():
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


from .backup import router as backup_router

app.include_router(backup_router)
