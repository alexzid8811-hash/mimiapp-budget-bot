from __future__ import annotations

from datetime import date, timedelta

from fastapi import Depends, HTTPException
from pydantic import Field, model_validator

from . import main as legacy
from . import clock, planning
from .validation import APIModel, DatedConditionsIn
from .auth import TelegramUser, current_user
from .payroll import PayrollConfig, payroll_events_between, payroll_for_accrual_month


app = legacy.app
_legacy_payday_days = legacy.payday_days
_legacy_period_recurring_income = legacy.period_recurring_income


class PayrollSettingsIn(DatedConditionsIn):
    payroll_enabled: bool = False
    salary_gross: float = Field(default=0, ge=0)
    bonus_gross: float = Field(default=0, ge=0)
    tax_rate: float = Field(default=13, ge=0, le=100)
    salary_day: int = Field(default=7, ge=1, le=31)
    advance_day: int = Field(default=22, ge=1, le=31)


class VacationIn(APIModel):
    start_date: date
    end_date: date
    amount: float = Field(gt=0)
    payment_date: date | None = None
    note: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def validate_dates(self) -> "VacationIn":
        if self.end_date < self.start_date:
            raise ValueError("Дата окончания отпуска должна быть не раньше даты начала")
        return self


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


def vacation_rows(user_id: int) -> list[dict]:
    return legacy.rows(
        "SELECT id,start_date,end_date,amount,payment_date,note,created_at "
        "FROM vacations WHERE user_id=? ORDER BY start_date DESC,id DESC",
        (user_id,),
    )


def payroll_payday_days(user_id: int) -> list[int]:
    cfg = payroll_settings(user_id)
    if int(cfg["payroll_enabled"]):
        return sorted({int(cfg["salary_day"]), int(cfg["advance_day"])})
    return _legacy_payday_days(user_id)


def payroll_period_recurring_income(user_id: int, start: date, end: date) -> float:
    return round(sum(planning.income_map(user_id, start, end, include_manual=False).values()), 2)


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
        today = clock.today()
        preview = payroll_for_accrual_month(today.year, today.month, payroll_config(uid), vacation_rows(uid))
    return {"settings": settings, "preview": preview}


@app.put("/api/payroll-settings")
def save_payroll_settings(payload: PayrollSettingsIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    with planning.change_conditions(uid, payload.effective_date, payroll=True) as con:
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
        today = clock.today()
        preview = payroll_for_accrual_month(today.year, today.month, payroll_config(uid), vacation_rows(uid))
    return {"settings": settings, "preview": preview}


@app.get("/api/vacations")
def get_vacations(user: TelegramUser = Depends(current_user)) -> list[dict]:
    uid = legacy.user_ready(user)
    return vacation_rows(uid)


@app.post("/api/vacations")
def create_vacation(payload: VacationIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    payment_date = payload.payment_date or (payload.start_date - timedelta(days=3))
    with legacy.connect() as con:
        cur = con.execute(
            "INSERT INTO vacations(user_id,start_date,end_date,amount,payment_date,note) VALUES(?,?,?,?,?,?)",
            (
                uid,
                payload.start_date.isoformat(),
                payload.end_date.isoformat(),
                payload.amount,
                payment_date.isoformat(),
                payload.note,
            ),
        )
        vacation_id = cur.lastrowid
    legacy.invalidate_current_auto_reserve(uid)
    return legacy.one("SELECT * FROM vacations WHERE id=? AND user_id=?", (vacation_id, uid)) or {}


@app.put("/api/vacations/{vacation_id}")
def update_vacation(vacation_id: int, payload: VacationIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    payment_date = payload.payment_date or (payload.start_date - timedelta(days=3))
    with legacy.connect() as con:
        cur = con.execute(
            "UPDATE vacations SET start_date=?,end_date=?,amount=?,payment_date=?,note=? WHERE id=? AND user_id=?",
            (
                payload.start_date.isoformat(),
                payload.end_date.isoformat(),
                payload.amount,
                payment_date.isoformat(),
                payload.note,
                vacation_id,
                uid,
            ),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Отпуск не найден")
    legacy.invalidate_current_auto_reserve(uid)
    return legacy.one("SELECT * FROM vacations WHERE id=? AND user_id=?", (vacation_id, uid)) or {}


@app.delete("/api/vacations/{vacation_id}")
def delete_vacation(vacation_id: int, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        cur = con.execute("DELETE FROM vacations WHERE id=? AND user_id=?", (vacation_id, uid))
        if cur.rowcount == 0:
            raise HTTPException(404, "Отпуск не найден")
    legacy.invalidate_current_auto_reserve(uid)
    return {"ok": True}
