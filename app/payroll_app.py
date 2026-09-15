from __future__ import annotations

from datetime import date

from fastapi import Depends
from pydantic import BaseModel, Field

from . import main as legacy
from .auth import TelegramUser, current_user
from .payroll import PayrollConfig, payroll_events_between, payroll_for_accrual_month


app = legacy.app
_legacy_payday_days = legacy.payday_days
_legacy_period_recurring_income = legacy.period_recurring_income


class PayrollSettingsIn(BaseModel):
    payroll_enabled: bool = False
    salary_gross: float = Field(default=0, ge=0)
    bonus_gross: float = Field(default=0, ge=0)
    tax_rate: float = Field(default=13, ge=0, le=100)
    salary_day: int = Field(default=7, ge=1, le=31)
    advance_day: int = Field(default=22, ge=1, le=31)


def payroll_settings(user_id: int) -> dict:
    return legacy.one(
        "SELECT payroll_enabled,salary_gross,bonus_gross,tax_rate,salary_day,advance_day "
        "FROM settings WHERE user_id=?",
        (user_id,),
    ) or {
        "payroll_enabled": 0,
        "salary_gross": 0,
        "bonus_gross": 0,
        "tax_rate": 13,
        "salary_day": 7,
        "advance_day": 22,
    }


def payroll_config(user_id: int) -> PayrollConfig:
    cfg = payroll_settings(user_id)
    return PayrollConfig(
        salary_gross=float(cfg["salary_gross"]),
        bonus_gross=float(cfg["bonus_gross"]),
        tax_rate=float(cfg["tax_rate"]),
        salary_day=int(cfg["salary_day"]),
        advance_day=int(cfg["advance_day"]),
    )


def payroll_payday_days(user_id: int) -> list[int]:
    cfg = payroll_settings(user_id)
    if int(cfg["payroll_enabled"]):
        return sorted({int(cfg["salary_day"]), int(cfg["advance_day"])})
    return _legacy_payday_days(user_id)


def payroll_period_recurring_income(user_id: int, start: date, end: date) -> float:
    cfg = payroll_settings(user_id)
    if not int(cfg["payroll_enabled"]):
        return _legacy_period_recurring_income(user_id, start, end)

    total = sum(float(event["amount"]) for event in payroll_events_between(start, end, payroll_config(user_id)))
    rules = legacy.rows(
        "SELECT amount,day_of_month FROM income_rules "
        "WHERE user_id=? AND active=1 AND kind='other'",
        (user_id,),
    )
    for rule in rules:
        for _ in legacy.occurrences([int(rule["day_of_month"])], start, end):
            total += float(rule["amount"])
    return round(total, 2)


# Functions defined in app.main resolve these names from app.main's globals at
# request time. Replacing them here upgrades the existing dashboard/forecast
# without duplicating the rest of the API.
legacy.payday_days = payroll_payday_days
legacy.period_recurring_income = payroll_period_recurring_income


@app.get("/api/payroll-settings")
def get_payroll_settings(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    settings = payroll_settings(uid)
    preview = None
    if int(settings["payroll_enabled"]):
        today = date.today()
        preview = payroll_for_accrual_month(today.year, today.month, payroll_config(uid))
    return {"settings": settings, "preview": preview}


@app.put("/api/payroll-settings")
def save_payroll_settings(payload: PayrollSettingsIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        con.execute(
            "UPDATE settings SET payroll_enabled=?,salary_gross=?,bonus_gross=?,tax_rate=?,salary_day=?,advance_day=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (
                int(payload.payroll_enabled),
                payload.salary_gross,
                payload.bonus_gross,
                payload.tax_rate,
                payload.salary_day,
                payload.advance_day,
                uid,
            ),
        )
    legacy.invalidate_current_auto_reserve(uid)
    settings = payroll_settings(uid)
    preview = None
    if int(settings["payroll_enabled"]):
        today = date.today()
        preview = payroll_for_accrual_month(today.year, today.month, payroll_config(uid))
    return {"settings": settings, "preview": preview}
