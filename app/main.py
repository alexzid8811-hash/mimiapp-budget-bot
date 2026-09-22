from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from . import clock, planning
from .validation import APIModel, DatedConditionsIn
from .savings import check_piggy_history, piggy_effect
from .money import amount as money_amount, cents
from .auth import TelegramUser, current_user
from .budget import current_period as current_period
from .budget import dashboard_numbers, reserve_needed_for_future
from .db import connect, ensure_user, init_db


app = FastAPI(title="Telegram Budget Mini App", version="0.1.0")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/")
def index() -> FileResponse:
    # The Desktop Telegram webview may otherwise combine a stale page shell with new scripts.
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store, max-age=0"})


class IncomeRuleIn(DatedConditionsIn):
    title: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0)
    day_of_month: int = Field(ge=1, le=31)
    kind: Literal["salary", "advance", "other"] = "other"
    is_payday: bool = False
    active: bool = True


class BillRuleIn(DatedConditionsIn):
    title: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0)
    day_of_month: int = Field(ge=1, le=31)
    category_id: int | None = None
    active: bool = True


class CategoryIn(APIModel):
    title: str = Field(min_length=1, max_length=40)
    emoji: str = Field(default="💳", min_length=1, max_length=8)


class CategoryOrderIn(APIModel):
    category_ids: list[int] = Field(min_length=1)


class TransactionIn(APIModel):
    type: Literal["expense", "income"]
    amount: float = Field(gt=0)
    tx_date: date = Field(default_factory=clock.today)
    category_id: int | None = None
    note: str = Field(default="", max_length=200)
    income_destination: Literal["daily", "buffer", "piggy"] = "daily"


class BillPaymentIn(APIModel):
    due_date: date


class BillPaymentEditIn(APIModel):
    amount: float = Field(ge=0)
    remainder_destination: Literal["budget", "piggy"] = "budget"


class SettingsIn(APIModel):
    currency: str = Field(default="RUB", pattern=r"^[A-Za-z]{3}$")
    initial_reserve: float = Field(default=0, ge=0)
    forecast_months: int = Field(default=4, ge=1, le=12)
    reminder_days: int = Field(default=3, ge=0, le=31)
    reminder_time: str = Field(default="10:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    morning_report_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def user_ready(user: TelegramUser) -> int:
    ensure_user(user.id, user.first_name, user.username)
    return user.id


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> dict | None:
    with connect() as con:
        r = con.execute(sql, params).fetchone()
        return dict(r) if r else None


def require_category(con, uid: int, category_id: int | None) -> None:
    if category_id is not None and not con.execute(
        "SELECT 1 FROM categories WHERE id=? AND user_id=?", (category_id, uid)
    ).fetchone():
        raise HTTPException(422, "Категория не найдена")


def payday_days(user_id: int) -> list[int]:
    data = rows(
        "SELECT DISTINCT day_of_month FROM income_rules WHERE user_id=? AND active=1 "
        "AND (is_payday=1 OR kind IN ('salary','advance')) ORDER BY day_of_month",
        (user_id,),
    )
    days = [int(r["day_of_month"]) for r in data]
    return days or [7, 22]


def period_recurring_income(user_id: int, start: date, end: date) -> float:
    """Scheduled income assigned to the daily-budget period that can use it."""
    return round(
        sum(planning.budget_income_map(user_id, start, end, include_manual=False).values()),
        2,
    )


def period_mandatory(user_id: int, start: date, end: date) -> float:
    return round(sum(planning.mandatory_map(user_id, start, end).values()), 2)


def actual_income(user_id: int, start: date, end: date) -> float:
    with connect() as con:
        r = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type='income' "
            "AND COALESCE(income_destination,'daily')='daily' AND tx_date BETWEEN ? AND ?",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchone()
        return round(float(r[0]), 2)


def discretionary_spent(user_id: int, start: date, end: date) -> float:
    with connect() as con:
        r = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions "
            "WHERE user_id=? AND type='expense' AND bill_rule_id IS NULL AND tx_date BETWEEN ? AND ?",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchone()
        return round(float(r[0]), 2)


def paid_mandatory_spent(user_id: int, start: date, end: date) -> float:
    """Actual obligatory payments, booked on the date money left the account."""
    with connect() as con:
        r = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions "
            "WHERE user_id=? AND type='expense' AND bill_rule_id IS NOT NULL "
            "AND tx_date BETWEEN ? AND ?",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchone()
        return round(float(r[0]), 2)


def reserve_balance_before(user_id: int, before_period: date) -> float:
    settings = one("SELECT initial_reserve FROM settings WHERE user_id=?", (user_id,)) or {}
    with connect() as con:
        r = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM reserve_movements WHERE user_id=? AND period_start < ?",
            (user_id, before_period.isoformat()),
        ).fetchone()
    return round(float(settings.get("initial_reserve", 0)) + float(r[0]), 2)


def future_reserve_target(user_id: int, current_end: date, count: int) -> tuple[float, list[dict]]:
    days = payday_days(user_id)
    periods = planning.next_periods(user_id, current_end + timedelta(days=1), max(2, count * max(1, len(days)) + 1))
    nets: list[float] = []
    detail: list[dict] = []
    for p in periods:
        income = period_recurring_income(user_id, p.start, p.end)
        bills = period_mandatory(user_id, p.start, p.end)
        net = round(income - bills, 2)
        nets.append(net)
        detail.append({"start": p.start.isoformat(), "end": p.end.isoformat(), "income": income, "mandatory": bills, "net": net})
    return reserve_needed_for_future(nets), detail


def ensure_auto_reserve(user_id: int, as_of: date) -> dict:
    period = planning.user_period(user_id, as_of)
    existing = one(
        "SELECT * FROM reserve_movements WHERE user_id=? AND period_start=? AND source='auto'",
        (user_id, period.start.isoformat()),
    )
    if existing:
        return existing

    settings = one("SELECT forecast_months FROM settings WHERE user_id=?", (user_id,)) or {"forecast_months": 4}
    reserve_before = reserve_balance_before(user_id, period.start)
    recurring_income = period_recurring_income(user_id, period.start, period.end)
    extra_income = actual_income(user_id, period.start, period.end)
    mandatory = period_mandatory(user_id, period.start, period.end)
    structural_free = recurring_income + extra_income - mandatory - piggy_effect(user_id, period.start, as_of)
    target, _ = future_reserve_target(user_id, period.end, int(settings["forecast_months"]))

    amount = 0.0
    reason = "Резерв не требуется"
    if structural_free < 0 and reserve_before > 0:
        release = min(reserve_before, -structural_free)
        amount = -round(release, 2)
        reason = "Автоподдержка периода с дефицитом обязательных платежей"
    elif structural_free > 0 and target > reserve_before:
        contribution = min(structural_free, target - reserve_before)
        amount = round(contribution, 2)
        reason = "Автокопилка для будущих обязательных платежей"

    with connect() as con:
        con.execute(
            "INSERT INTO reserve_movements(user_id,period_start,amount,reason,source) VALUES(?,?,?,?, 'auto')",
            (user_id, period.start.isoformat(), amount, reason),
        )
    return one(
        "SELECT * FROM reserve_movements WHERE user_id=? AND period_start=? AND source='auto'",
        (user_id, period.start.isoformat()),
    ) or {"amount": amount, "reason": reason}


def invalidate_current_auto_reserve(user_id: int) -> None:
    today = clock.today()
    period = planning.user_period(user_id, today)
    with connect() as con:
        con.execute(
            "DELETE FROM reserve_movements WHERE user_id=? AND source='auto' AND period_start>=?",
            (user_id, period.start.isoformat()),
        )


@app.get("/api/bootstrap")
def bootstrap(user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    return {
        "budget_timezone": clock.budget_timezone(),
        "today": clock.today().isoformat(),
        "user": {"id": user.id, "first_name": user.first_name, "username": user.username},
        "settings": one("SELECT currency,initial_reserve,forecast_months,reminder_days,reminder_time,morning_report_time FROM settings WHERE user_id=?", (uid,)),
        "income_rules": rows("SELECT * FROM income_rules WHERE user_id=? AND archived=0 ORDER BY day_of_month,id", (uid,)),
        "bill_rules": rows("SELECT * FROM bill_rules WHERE user_id=? AND archived=0 ORDER BY day_of_month,id", (uid,)),
        "categories": rows(
            "SELECT * FROM categories WHERE user_id=? ORDER BY sort_order,id", (uid,)
        ),
    }


@app.get("/api/dashboard")
def dashboard(user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    today = clock.today()
    period = planning.user_period(uid, today)
    movement = ensure_auto_reserve(uid, today)
    reserve_before = reserve_balance_before(uid, period.start)
    reserve_amount = float(movement["amount"])
    reserve_in = max(0.0, reserve_amount)
    reserve_out = max(0.0, -reserve_amount)

    recurring = period_recurring_income(uid, period.start, period.end)
    extras = actual_income(uid, period.start, period.end)
    mandatory = period_mandatory(uid, period.start, period.end)
    spent = discretionary_spent(uid, period.start, today)
    spent_today = discretionary_spent(uid, today, today)
    remaining_days = (period.end - today).days + 1
    piggy = piggy_effect(uid, period.start, today)
    numbers = dashboard_numbers(recurring + extras - piggy, mandatory, spent, reserve_in, reserve_out, remaining_days)

    settings = one("SELECT forecast_months FROM settings WHERE user_id=?", (uid,)) or {"forecast_months": 4}
    target, forecast = future_reserve_target(uid, period.end, int(settings["forecast_months"]))
    reserve_now = round(reserve_before + reserve_amount, 2)

    deficit = max(0.0, mandatory + piggy - (recurring + extras + reserve_out))
    return {
        "today": today.isoformat(),
        "period": {"start": period.start.isoformat(), "end": period.end.isoformat(), "days_left": remaining_days},
        "income": round(recurring + extras, 2),
        "mandatory": mandatory,
        "spent": spent,
        "spent_today": spent_today,
        "period_budget": numbers["period_budget"],
        "remaining": numbers["remaining"],
        "daily_available": numbers["daily"],
        "reserve": {
            "before": reserve_before,
            "auto_movement": reserve_amount,
            "balance": reserve_now,
            "future_target": target,
            "reason": movement.get("reason", ""),
        },
        "deficit": round(deficit, 2),
        "forecast": forecast[:8],
    }


@app.get("/api/transactions")
def transactions(limit: int = 80, user: TelegramUser = Depends(current_user)) -> list[dict]:
    uid = user_ready(user)
    return rows(
        "SELECT t.*, c.title AS category_title, c.emoji AS category_emoji, b.title AS bill_title, "
        "p.amount AS remainder_amount, "
        "CASE WHEN p.id IS NULL THEN 'budget' ELSE 'piggy' END AS remainder_destination "
        "FROM transactions t LEFT JOIN categories c ON c.id=t.category_id AND c.user_id=t.user_id "
        "LEFT JOIN bill_rules b ON b.id=t.bill_rule_id AND b.user_id=t.user_id "
        "LEFT JOIN piggy_bank_movements p ON p.user_id=t.user_id AND p.bill_payment_id=t.id "
        "WHERE t.user_id=? ORDER BY tx_date DESC,id DESC LIMIT ?",
        (uid, min(max(limit, 1), 300)),
    )


@app.post("/api/transactions")
def create_transaction(payload: TransactionIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    if payload.tx_date > clock.today():
        raise HTTPException(422, "Дата операции не может быть в будущем")
    with connect() as con:
        require_category(con, uid, payload.category_id)
        cur = con.execute(
            "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note,income_destination) VALUES(?,?,?,?,?,?,?)",
            (uid, payload.type, payload.amount, payload.tx_date.isoformat(), payload.category_id,
             payload.note, payload.income_destination if payload.type == "income" else "daily"),
        )
        tx_id = cur.lastrowid
        if payload.type == "income" and payload.income_destination == "piggy":
            con.execute(
                "INSERT INTO piggy_bank_movements"
                "(user_id,direction,amount,movement_date,note,source,income_transaction_id) "
                "VALUES(?, 'deposit', ?, ?, ?, 'external', ?)",
                (uid, payload.amount, payload.tx_date.isoformat(), payload.note, tx_id),
            )
            check_piggy_history(con, uid)
    invalidate_current_auto_reserve(uid)
    return one("SELECT * FROM transactions WHERE id=? AND user_id=?", (tx_id, uid)) or {}


@app.put("/api/transactions/{tx_id}")
def update_transaction(
    tx_id: int, payload: TransactionIn, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = user_ready(user)
    if payload.tx_date > clock.today():
        raise HTTPException(422, "Дата операции не может быть в будущем")
    with connect() as con:
        require_category(con, uid, payload.category_id)
        current = con.execute(
            "SELECT bill_rule_id FROM transactions WHERE id=? AND user_id=?", (tx_id, uid)
        ).fetchone()
        if current is None:
            raise HTTPException(404, "Операция не найдена")
        if current["bill_rule_id"] is not None:
            raise HTTPException(422, "Обязательный платёж изменяется через его отдельную форму")
        con.execute(
            "UPDATE transactions SET type=?,amount=?,tx_date=?,category_id=?,note=?,income_destination=? "
            "WHERE id=? AND user_id=?",
            (
                payload.type, payload.amount, payload.tx_date.isoformat(), payload.category_id,
                payload.note, payload.income_destination if payload.type == "income" else "daily", tx_id, uid,
            ),
        )
        con.execute("DELETE FROM piggy_bank_movements WHERE user_id=? AND income_transaction_id=?", (uid, tx_id))
        if payload.type == "income" and payload.income_destination == "piggy":
            con.execute(
                "INSERT INTO piggy_bank_movements"
                "(user_id,direction,amount,movement_date,note,source,income_transaction_id) "
                "VALUES(?, 'deposit', ?, ?, ?, 'external', ?)",
                (uid, payload.amount, payload.tx_date.isoformat(), payload.note, tx_id),
            )
        check_piggy_history(con, uid)
    invalidate_current_auto_reserve(uid)
    return one("SELECT * FROM transactions WHERE id=? AND user_id=?", (tx_id, uid)) or {}


@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: int, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    try:
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM piggy_bank_movements WHERE user_id=? AND income_transaction_id=?", (uid, tx_id))
            cur = con.execute("DELETE FROM transactions WHERE id=? AND user_id=?", (tx_id, uid))
            if cur.rowcount == 0:
                raise HTTPException(404, "Operation not found")
            check_piggy_history(con, uid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    invalidate_current_auto_reserve(uid)
    return {"ok": True}


@app.get("/api/plan")
def plan(user: TelegramUser = Depends(current_user)) -> list[dict]:
    uid = user_ready(user)
    today = clock.today()
    period = planning.user_period(uid, today)
    categories = {row['id']: row for row in rows("SELECT * FROM categories WHERE user_id=?", (uid,))}
    events = planning.bill_events(uid, period.start, period.end)
    for event in events:
        category = categories.get(event.get('category_id'), {})
        event.update(category_title=category.get('title'), category_emoji=category.get('emoji'))
    return events


@app.post("/api/bills/{bill_id}/pay")
def pay_bill(bill_id: int, payload: BillPaymentIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    bill = one("SELECT * FROM bill_rules WHERE id=? AND user_id=?", (bill_id, uid))
    if not bill:
        raise HTTPException(404, "Bill not found")
    event = next((e for e in planning.bill_events(uid, payload.due_date, payload.due_date) if e['id'] == bill_id), None)
    if event is None:
        raise HTTPException(422, "На эту дату обязательный платёж не запланирован")
    try:
        with connect() as con:
            require_category(con, uid, event.get("category_id"))
            cur = con.execute(
                "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note,bill_rule_id,bill_due_date,bill_planned_amount) "
                "VALUES(?, 'expense', ?, ?, ?, ?, ?, ?, ?)",
                (
                    uid, event["amount"], clock.today().isoformat(), event.get("category_id"),
                    event["title"], bill_id, payload.due_date.isoformat(), event["amount"],
                ),
            )
            tx_id = cur.lastrowid
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "This bill occurrence is already marked paid") from exc
    invalidate_current_auto_reserve(uid)
    return one("SELECT * FROM transactions WHERE id=?", (tx_id,)) or {}


@app.put("/api/bill-payments/{payment_id}")
def edit_bill_payment(
    payment_id: int, payload: BillPaymentEditIn, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = user_ready(user)
    try:
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            payment = con.execute(
                "SELECT * FROM transactions WHERE id=? AND user_id=? AND type='expense' "
                "AND bill_rule_id IS NOT NULL AND bill_due_date IS NOT NULL",
                (payment_id, uid),
            ).fetchone()
            if payment is None:
                raise HTTPException(404, "Оплаченный обязательный платёж не найден")

            due_date = date.fromisoformat(payment["bill_due_date"])
            planned = payment["bill_planned_amount"]
            if planned is None:
                planned = planning.bill_planned_amount(uid, payment["bill_rule_id"], due_date)
            if planned is None:
                planned = payment["amount"]

            planned_cents = cents(planned)
            actual_cents = cents(payload.amount)
            saved_cents = max(0, planned_cents - actual_cents) if payload.remainder_destination == "piggy" else 0
            saved_amount = money_amount(saved_cents)

            con.execute(
                "UPDATE transactions SET amount=?,bill_planned_amount=? WHERE id=? AND user_id=?",
                (money_amount(actual_cents), money_amount(planned_cents), payment_id, uid),
            )
            existing = con.execute(
                "SELECT id FROM piggy_bank_movements WHERE user_id=? AND bill_payment_id=?",
                (uid, payment_id),
            ).fetchone()
            if saved_cents:
                movement_date = str(payment["tx_date"])
                note = f"Остаток от обязательного платежа: {payment['note']}"[:160]
                if existing:
                    con.execute(
                        "UPDATE piggy_bank_movements SET amount=?,movement_date=?,note=?,source='daily_budget' WHERE id=?",
                        (saved_amount, movement_date, note, existing["id"]),
                    )
                else:
                    con.execute(
                        "INSERT INTO piggy_bank_movements"
                        "(user_id,direction,amount,movement_date,note,source,bill_payment_id) VALUES(?, 'deposit', ?, ?, ?, 'daily_budget', ?)",
                        (uid, saved_amount, movement_date, note, payment_id),
                    )
            elif existing:
                con.execute("DELETE FROM piggy_bank_movements WHERE id=?", (existing["id"],))

            check_piggy_history(con, uid)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    invalidate_current_auto_reserve(uid)
    return {
        "payment": one("SELECT * FROM transactions WHERE id=? AND user_id=?", (payment_id, uid)),
        "planned_amount": money_amount(planned_cents),
        "remainder_destination": payload.remainder_destination if saved_cents else "budget",
        "remainder_amount": saved_amount,
    }


@app.post("/api/income-rules")
def add_income(payload: IncomeRuleIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    with planning.change_conditions(uid, payload.effective_date) as con:
        cur = con.execute(
            "INSERT INTO income_rules(user_id,title,amount,day_of_month,kind,is_payday,active) VALUES(?,?,?,?,?,?,?)",
            (uid, payload.title, payload.amount, payload.day_of_month, payload.kind, int(payload.is_payday), int(payload.active)),
        )
        rid = cur.lastrowid
    invalidate_current_auto_reserve(uid)
    return one("SELECT * FROM income_rules WHERE id=?", (rid,)) or {}


@app.put("/api/income-rules/{rule_id}")
def edit_income(rule_id: int, payload: IncomeRuleIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    with planning.change_conditions(uid, payload.effective_date, table="income_rules", record_id=rule_id) as con:
        cur = con.execute(
            "UPDATE income_rules SET title=?,amount=?,day_of_month=?,kind=?,is_payday=?,active=? WHERE id=? AND user_id=?",
            (payload.title, payload.amount, payload.day_of_month, payload.kind, int(payload.is_payday), int(payload.active), rule_id, uid),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Income rule not found")
    invalidate_current_auto_reserve(uid)
    return one("SELECT * FROM income_rules WHERE id=?", (rule_id,)) or {}


@app.delete("/api/income-rules/{rule_id}")
def delete_income(rule_id: int, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    with planning.change_conditions(uid) as con:
        con.execute("UPDATE income_rules SET active=0,archived=1 WHERE id=? AND user_id=?", (rule_id, uid))
    invalidate_current_auto_reserve(uid)
    return {"ok": True}


@app.post("/api/bill-rules")
def add_bill(payload: BillRuleIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    with planning.change_conditions(uid, payload.effective_date) as con:
        require_category(con, uid, payload.category_id)
        cur = con.execute(
            "INSERT INTO bill_rules(user_id,title,amount,day_of_month,category_id,active) VALUES(?,?,?,?,?,?)",
            (uid, payload.title, payload.amount, payload.day_of_month, payload.category_id, int(payload.active)),
        )
        rid = cur.lastrowid
    invalidate_current_auto_reserve(uid)
    return one("SELECT * FROM bill_rules WHERE id=?", (rid,)) or {}


@app.put("/api/bill-rules/{rule_id}")
def edit_bill(rule_id: int, payload: BillRuleIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    with planning.change_conditions(uid, payload.effective_date, table="bill_rules", record_id=rule_id) as con:
        require_category(con, uid, payload.category_id)
        cur = con.execute(
            "UPDATE bill_rules SET title=?,amount=?,day_of_month=?,category_id=?,active=? WHERE id=? AND user_id=?",
            (payload.title, payload.amount, payload.day_of_month, payload.category_id, int(payload.active), rule_id, uid),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Bill rule not found")
    invalidate_current_auto_reserve(uid)
    return one("SELECT * FROM bill_rules WHERE id=?", (rule_id,)) or {}


@app.delete("/api/bill-rules/{rule_id}")
def delete_bill(rule_id: int, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    with planning.change_conditions(uid) as con:
        con.execute("UPDATE bill_rules SET active=0,archived=1 WHERE id=? AND user_id=?", (rule_id, uid))
    invalidate_current_auto_reserve(uid)
    return {"ok": True}


@app.post("/api/categories")
def add_category(payload: CategoryIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    try:
        with connect() as con:
            next_order = con.execute(
                "SELECT COALESCE(MAX(sort_order),0)+1 FROM categories WHERE user_id=?",
                (uid,),
            ).fetchone()[0]
            cur = con.execute(
                "INSERT INTO categories(user_id,title,emoji,sort_order) VALUES(?,?,?,?)",
                (uid, payload.title, payload.emoji, next_order),
            )
            rid = cur.lastrowid
    except Exception as exc:
        raise HTTPException(409, "Category already exists") from exc
    return one("SELECT * FROM categories WHERE id=?", (rid,)) or {}


@app.put("/api/categories/order")
def reorder_categories(
    payload: CategoryOrderIn, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = user_ready(user)
    category_ids = payload.category_ids
    if len(category_ids) != len(set(category_ids)):
        raise HTTPException(422, "Категории не должны повторяться")
    with connect() as con:
        current_ids = {
            int(row[0])
            for row in con.execute("SELECT id FROM categories WHERE user_id=?", (uid,))
        }
        if set(category_ids) != current_ids:
            raise HTTPException(422, "Список категорий изменился. Обновите страницу")
        for position, category_id in enumerate(category_ids, start=1):
            con.execute(
                "UPDATE categories SET sort_order=? WHERE id=? AND user_id=?",
                (position, category_id, uid),
            )
    return {"ok": True}


@app.put("/api/settings")
def save_settings(payload: SettingsIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = user_ready(user)
    with connect() as con:
        con.execute(
            "UPDATE settings SET currency=?,initial_reserve=?,forecast_months=?,reminder_days=?,reminder_time=?,morning_report_time=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            (payload.currency.upper(), payload.initial_reserve, payload.forecast_months, payload.reminder_days, payload.reminder_time, payload.morning_report_time, uid),
        )
    invalidate_current_auto_reserve(uid)
    return one("SELECT currency,initial_reserve,forecast_months,reminder_days,reminder_time,morning_report_time FROM settings WHERE user_id=?", (uid,)) or {}
