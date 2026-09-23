from __future__ import annotations

from datetime import date, timedelta

from fastapi import Depends, HTTPException
from pydantic import Field, model_validator

from . import main as legacy
from . import clock, planning
from .validation import APIModel
from .auth import TelegramUser, current_user
from .payroll import PayrollConfig, payroll_for_accrual_month


app = legacy.app


class PayrollSettingsIn(APIModel):
    payroll_enabled: bool = False
    salary_gross: float = Field(default=0, ge=0)
    bonus_gross: float = Field(default=0, ge=0)
    tax_rate: float = Field(default=13, ge=0, le=100)
    salary_day: int = Field(default=7, ge=1, le=31)
    advance_day: int = Field(default=22, ge=1, le=31)


class PayrollChangeIn(APIModel):
    effective_month: date
    salary_gross: float = Field(ge=0)
    bonus_gross: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_effective_month(self) -> "PayrollChangeIn":
        if self.effective_month.day != 1:
            raise ValueError("Изменение зарплаты начинается с первого дня месяца")
        if self.effective_month <= clock.today().replace(day=1):
            raise ValueError("Выберите будущий месяц")
        return self


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


def payroll_changes(user_id: int) -> list[dict]:
    return legacy.rows(
        "SELECT id,effective_month,salary_gross,bonus_gross FROM payroll_changes "
        "WHERE user_id=? ORDER BY effective_month,id",
        (user_id,),
    )


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


@app.get("/api/payroll-settings")
def get_payroll_settings(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    settings = payroll_settings(uid)
    preview = None
    if int(settings["payroll_enabled"]):
        today = clock.today()
        preview = payroll_for_accrual_month(today.year, today.month, payroll_config(uid), vacation_rows(uid))
    return {"settings": settings, "preview": preview, "changes": payroll_changes(uid)}


@app.post("/api/payroll-changes")
def save_payroll_change(payload: PayrollChangeIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        con.execute(
            "INSERT INTO payroll_changes(user_id,effective_month,salary_gross,bonus_gross) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,effective_month) DO UPDATE SET "
            "salary_gross=excluded.salary_gross,bonus_gross=excluded.bonus_gross",
            (
                uid,
                payload.effective_month.isoformat(),
                payload.salary_gross,
                payload.bonus_gross,
            ),
        )
    # The current reserve movement deliberately remains untouched.  The
    # scheduled sum is used by the forecast now and by the buffer once that
    # future budget period begins.
    return next(
        change for change in payroll_changes(uid)
        if change["effective_month"] == payload.effective_month.isoformat()
    )


@app.put("/api/payroll-changes/{change_id}")
def update_payroll_change(
    change_id: int, payload: PayrollChangeIn, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        current = con.execute(
            "SELECT id FROM payroll_changes WHERE id=? AND user_id=?", (change_id, uid)
        ).fetchone()
        if current is None:
            raise HTTPException(404, "Изменение зарплаты не найдено")
        duplicate = con.execute(
            "SELECT id FROM payroll_changes WHERE user_id=? AND effective_month=? AND id<>?",
            (uid, payload.effective_month.isoformat(), change_id),
        ).fetchone()
        if duplicate is not None:
            raise HTTPException(409, "На этот месяц уже запланировано изменение")
        con.execute(
            "UPDATE payroll_changes SET effective_month=?,salary_gross=?,bonus_gross=? "
            "WHERE id=? AND user_id=?",
            (
                payload.effective_month.isoformat(),
                payload.salary_gross,
                payload.bonus_gross,
                change_id,
                uid,
            ),
        )
    # Periods before the selected month remain unchanged; the next forecast
    # reads this schedule and rebuilds the buffer from the future change.
    return next(change for change in payroll_changes(uid) if change["id"] == change_id)


@app.delete("/api/payroll-changes/{change_id}")
def delete_payroll_change(change_id: int, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        cur = con.execute("DELETE FROM payroll_changes WHERE id=? AND user_id=?", (change_id, uid))
        if cur.rowcount == 0:
            raise HTTPException(404, "Изменение зарплаты не найдено")
    return {"ok": True}


@app.put("/api/payroll-settings")
def save_payroll_settings(payload: PayrollSettingsIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    with planning.change_conditions(uid, payroll=True) as con:
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
    return {"settings": settings, "preview": preview, "changes": payroll_changes(uid)}


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
