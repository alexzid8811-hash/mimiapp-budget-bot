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
from .money import amount, cents


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
    source: Literal["external", "daily_budget"] = "external"


class CashflowIncomeOverrideIn(APIModel):
    amount: float = Field(ge=0)


def cashflow_settings(user_id: int) -> dict:
    data = legacy.one(
        "SELECT cashflow_enabled,cashflow_start_date,cashflow_start_capital,forecast_months "
        "FROM settings WHERE user_id=?",
        (user_id,),
    ) or {}
    return {
        "cashflow_enabled": int(data.get("cashflow_enabled") or 0),
        "start_date": data.get("cashflow_start_date") or clock.today().isoformat(),
        "start_capital": float(data.get("cashflow_start_capital") or 0),
        "forecast_months": int(data.get("forecast_months") or 4),
    }


def _add(target: dict[date, float], day: date, amount: float) -> None:
    target[day] = round(target.get(day, 0.0) + float(amount), 2)


def planned_income_map(
    user_id: int,
    start: date,
    end: date,
    payroll_changes_through: date | None = None,
) -> dict[date, float]:
    # Keep the real receipt date in history, but do not make a salary or
    # advance spendable until the following calendar day.
    return planning.budget_income_map(
        user_id, start, end, payroll_changes_through=payroll_changes_through
    )


def planned_mandatory_map(user_id: int, start: date, end: date) -> dict[date, float]:
    return planning.unpaid_mandatory_map(user_id, start, end)


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
        "SELECT id,direction,amount,movement_date,note,source,created_at FROM piggy_bank_movements "
        "WHERE user_id=? ORDER BY movement_date DESC,id DESC LIMIT ?",
        (user_id, min(max(limit, 1), 300)),
    )
    return {"balance": piggy_bank_balance(user_id), "movements": movements}


def payday_boundaries(user_id: int, start: date, end: date) -> list[dict]:
    return planning.payday_boundaries(user_id, start, end)


def cashflow_income_overrides(user_id: int, start: date, end: date) -> dict[date, float]:
    """Return overrides by budget-period start.

    Older backups stored the actual payday as period_start. Accept those rows
    as the preceding day so a deployed update does not discard an already
    entered correction; new rows use the budget-period start directly.
    """
    values = legacy.rows(
        "SELECT period_start,amount FROM cashflow_income_overrides "
        "WHERE user_id=? AND period_start BETWEEN ? AND ? ORDER BY period_start",
        (user_id, (start - timedelta(days=1)).isoformat(), end.isoformat()),
    )
    current_starts = {
        row["date"] for row in payday_boundaries(user_id, start, end)
    }
    result: dict[date, float] = {}
    for row in values:
        stored_start = date.fromisoformat(row["period_start"])
        budget_start = stored_start if stored_start in current_starts else stored_start + timedelta(days=1)
        if not start <= budget_start <= end:
            continue
        # A newly saved value at the real budget start wins over a legacy row.
        if budget_start not in result or stored_start == budget_start:
            result[budget_start] = round(float(row["amount"]), 2)
    return result



def manual_buffer_income(user_id: int, start: date, end: date) -> float:
    """Income deliberately routed to the buffer, not to the daily card budget."""
    row = legacy.one(
        "SELECT COALESCE(SUM(amount),0) AS total FROM transactions "
        "WHERE user_id=? AND type='income' AND COALESCE(income_destination,'daily')='buffer' "
        "AND tx_date BETWEEN ? AND ?",
        (user_id, start.isoformat(), end.isoformat()),
    )
    return round(float(row["total"]) if row else 0.0, 2)


def daily_income_in_period(user_id: int, start: date, end: date) -> float:
    """Manual income reserved for card spending in this pay period."""
    row = legacy.one(
        "SELECT COALESCE(SUM(amount),0) AS total FROM transactions "
        "WHERE user_id=? AND type='income' AND COALESCE(income_destination,'daily')='daily' "
        "AND tx_date BETWEEN ? AND ?",
        (user_id, start.isoformat(), end.isoformat()),
    )
    return round(float(row["total"]) if row else 0.0, 2)


def recorded_income_map(user_id: int, start: date, end: date) -> dict[date, float]:
    """Manual income transactions that must not be forecast a second time."""
    rows = legacy.rows(
        "SELECT tx_date,COALESCE(SUM(amount),0) AS total FROM transactions "
        "WHERE user_id=? AND type='income' AND tx_date BETWEEN ? AND ? "
        "GROUP BY tx_date",
        (user_id, start.isoformat(), end.isoformat()),
    )
    return {date.fromisoformat(row["tx_date"]): round(float(row["total"]), 2) for row in rows}

def cashflow_periods(user_id: int, today: date, horizon_end: date) -> list[dict]:
    starts = [{"date": today, "kind": "сейчас"}, *payday_boundaries(user_id, today + timedelta(days=1), horizon_end)]
    return [
        {
            **item,
            "end": starts[index + 1]["date"] - timedelta(days=1)
            if index + 1 < len(starts)
            else horizon_end,
        }
        for index, item in enumerate(starts)
        if item["date"] <= horizon_end
    ]


def apply_cashflow_income_overrides(
    user_id: int,
    *,
    today: date,
    horizon_end: date,
    income: dict[date, float],
) -> dict[date, float]:
    """Adjust future income so each edited period has the requested total."""
    adjusted = dict(income)
    overrides = cashflow_income_overrides(user_id, today + timedelta(days=1), horizon_end)
    for period in cashflow_periods(user_id, today, horizon_end)[1:]:
        period_start = period["date"]
        if period_start not in overrides:
            continue
        period_end = period["end"]
        planned = round(
            sum(amount for day, amount in income.items() if period_start <= day <= period_end), 2
        )
        _add(adjusted, period_start, overrides[period_start] - planned)
    return adjusted


def cashflow_period_rows(
    user_id: int,
    *,
    today: date,
    horizon_end: date,
    opening_balance_before_today_spend: float,
    daily_target: float,
    today_target: float | None = None,
    spent_today: float = 0,
    income_overrides: dict[date, float] | None = None,
) -> list[dict]:
    """Build buffer rows without rewriting periods before an edited payment.

    A correction starts a new daily-budget segment on that payment date.  Its
    reduced (or increased) safe limit is applied only to that period and later
    ones; rows before it retain the plan that was already shown to the user.
    """
    periods = cashflow_periods(user_id, today, horizon_end)
    income = planned_income_map(user_id, today + timedelta(days=1), horizon_end)
    mandatory = planned_mandatory_map(user_id, today + timedelta(days=1), horizon_end)
    overrides = income_overrides or {}
    daily_by_period = [round(daily_target, 2) for _ in periods]
    if periods and today_target is not None:
        daily_by_period[0] = round(today_target, 2)

    # Apply edits in chronological order.  Later edits deliberately do not
    # participate in an earlier segment calculation, otherwise changing (for
    # example) December would retroactively change September--November.
    for index, item in enumerate(periods):
        period_start = item["date"]
        if index == 0 or period_start not in overrides:
            continue

        balance_before = round(opening_balance_before_today_spend, 2)
        for previous_index, previous in enumerate(periods[:index]):
            previous_start = previous["date"]
            previous_end = previous["end"]
            planned_income = round(
                sum(value for day, value in income.items() if previous_start <= day <= previous_end),
                2,
            )
            effective_income = (
                overrides[previous_start]
                if previous_index > 0 and previous_start in overrides
                else planned_income
            )
            bills = round(
                sum(value for day, value in mandatory.items() if previous_start <= day <= previous_end),
                2,
            )
            balance_before = round(
                balance_before + effective_income - bills
                - daily_by_period[previous_index] * ((previous_end - previous_start).days + 1),
                2,
            )

        # calculate_cashflow_plan treats its opening amount as already
        # containing today's flows, so add the payment and bills on the first
        # day of this new segment explicitly.
        period_planned_income = sum(
            value for day, value in income.items() if period_start <= day <= item["end"]
        )
        opening = round(
            balance_before
            + float(income.get(period_start, 0.0))
            + overrides[period_start]
            - period_planned_income
            - float(mandatory.get(period_start, 0.0)),
            2,
        )
        segment_income = {
            day: value for day, value in income.items()
            if period_start < day <= horizon_end
        }
        segment_mandatory = {
            day: value for day, value in mandatory.items()
            if period_start < day <= horizon_end
        }
        segment_plan = calculate_cashflow_plan(
            today=period_start,
            horizon_end=horizon_end,
            opening_balance_before_today_spend=opening,
            spent_today=0,
            income_by_date=segment_income,
            mandatory_by_date=segment_mandatory,
        )
        for later_index in range(index, len(daily_by_period)):
            daily_by_period[later_index] = round(segment_plan.daily_target, 2)

    buffer_before = 0.0
    result: list[dict] = []
    for index, item in enumerate(periods):
        period_start = item["date"]
        period_end = item["end"]
        days = (period_end - period_start).days + 1
        planned_income = round(
            sum(value for day, value in income.items() if period_start <= day <= period_end), 2
        )
        overridden = index > 0 and period_start in overrides
        effective_income = overrides[period_start] if overridden else planned_income
        received = round(
            (opening_balance_before_today_spend if index == 0 else 0.0) + effective_income, 2
        )
        bills = round(
            sum(value for day, value in mandatory.items() if period_start <= day <= period_end), 2
        )
        free = round(received - bills, 2)
        period_daily = daily_by_period[index]
        if index == 0 and today_target is None:
            # Standalone callers may only know the recalculated future daily
            # amount.  Today's real spending must still leave the account in
            # this row, including an overspend.
            planned_spending = round(
                max(float(spent_today), period_daily) + period_daily * max(0, days - 1),
                2,
            )
        elif index == 0:
            # When the original allowance is available, keep the protected
            # current-period buffer stable.  Overspending is redistributed
            # over later days instead of silently consuming that buffer.
            planned_spending = round(float(today_target) * days, 2)
        else:
            planned_spending = round(period_daily * days, 2)
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
                "planned_received": round(
                    (opening_balance_before_today_spend if index == 0 else 0.0) + planned_income, 2
                ),
                "income_overridden": overridden,
                "received_editable": index > 0,
                "days": days,
                "mandatory": bills,
                "free": free,
                "daily": period_daily,
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
    # The start capital is the complete real account balance at the beginning
    # of the selected date.  Money received before that moment (including
    # vacation pay) must already be inside it and must never be added again.
    start_capital = float(settings["start_capital"])
    months = max(1, min(12, int(settings["forecast_months"])))
    horizon_end = add_months(today, months)

    # Past scheduled income is only a forecast until the user records that it
    # was actually received. Otherwise an unpaid salary/vacation payment would
    # silently inflate the real balance carried from the starting capital.
    # An override belongs to a budget period that starts the day after the
    # actual payday.  The cash is already on the account on the payday itself,
    # even though it becomes available for daily spending only tomorrow.
    confirmed_overrides = cashflow_income_overrides(
        user_id, start_date, today + timedelta(days=1)
    )
    actual_income_history = legacy.actual_income(user_id, start_date, today) + sum(confirmed_overrides.values())
    paid_mandatory_history = legacy.paid_mandatory_spent(user_id, start_date, today)
    unpaid_mandatory_history = planned_mandatory_map(user_id, start_date, today)
    spent_history = legacy.discretionary_spent(user_id, start_date, today)

    piggy_effect = piggy_bank_effect(user_id, start_date, today)
    current_cash = round(
        start_capital + actual_income_history - paid_mandatory_history - spent_history - piggy_effect,
        2,
    )
    reserved_mandatory = round(sum(unpaid_mandatory_history.values()), 2)
    available_cash = round(current_cash - reserved_mandatory, 2)
    spent_today = legacy.discretionary_spent(user_id, today, today)
    opening_before_today_spend = round(available_cash + spent_today, 2)

    tomorrow = today + timedelta(days=1)
    planned_income_future = (
        planned_income_map(user_id, tomorrow, horizon_end)
        if tomorrow <= horizon_end
        else {}
    )
    income_future = apply_cashflow_income_overrides(
        user_id,
        today=today,
        horizon_end=horizon_end,
        income=planned_income_future,
    )
    # A period override is the corrected total income for that whole payday
    # period.  Once its start date arrives, it becomes confirmed cash.  Remove
    # the remaining planned items from the same period so vacation pay or an
    # additional rule inside it cannot be counted for a second time.
    for overridden_start in confirmed_overrides:
        overridden_end = planning.user_period(user_id, overridden_start).end
        for income_day in list(income_future):
            if overridden_start <= income_day <= overridden_end:
                income_future.pop(income_day)
                planned_income_future.pop(income_day)
    mandatory_future = planned_mandatory_map(user_id, tomorrow, horizon_end) if tomorrow <= horizon_end else {}

    # A correction belongs to its payment period and later periods.  Keep the
    # already-open period on the plan that existed before the correction; the
    # period table applies the adjusted amount from the corrected period on.
    current_plan = calculate_cashflow_plan(
        today=today,
        horizon_end=horizon_end,
        opening_balance_before_today_spend=opening_before_today_spend,
        spent_today=spent_today,
        income_by_date=planned_income_future,
        mandatory_by_date=mandatory_future,
    )
    plan = calculate_cashflow_plan(
        today=today,
        horizon_end=horizon_end,
        opening_balance_before_today_spend=opening_before_today_spend,
        spent_today=spent_today,
        income_by_date=income_future,
        mandatory_by_date=mandatory_future,
    )

    period = planning.user_period(user_id, today)
    period_anchor = max(period.start, start_date)
    spent_period = legacy.discretionary_spent(user_id, period_anchor, today)
    transferred_period = piggy_bank_effect(user_id, period_anchor, today)
    days_left = max(1, (period.end - today).days + 1)
    mandatory_period = round(
        reserved_mandatory
        + sum(value for day, value in mandatory_future.items() if day <= period.end),
        2,
    )
    # Reconstruct the allocation made at the beginning of the current period.
    # Replanning from today's already-reduced cash and then subtracting the
    # period's expenses once more double-counted yesterday's spending after
    # midnight.  The original allocation stays fixed until the next payday;
    # real expenses and explicit daily-budget transfers reduce only the card.
    # Manual income routed to the card belongs entirely to this pay period.
    # Exclude it from the long-horizon baseline, then add it back only to the
    # current period budget.  Otherwise the generic forecast would reserve
    # most of it for future periods and incorrectly increase the buffer.
    daily_income_period = daily_income_in_period(user_id, period_anchor, today)
    anchor_opening = round(
        available_cash + spent_period + transferred_period - daily_income_period,
        2,
    )
    anchor_horizon_end = add_months(period_anchor, months)
    anchor_tomorrow = period_anchor + timedelta(days=1)
    anchor_income = (
        planned_income_map(
            user_id,
            anchor_tomorrow,
            anchor_horizon_end,
            payroll_changes_through=period_anchor.replace(day=1),
        )
        if anchor_tomorrow <= anchor_horizon_end else {}
    )
    anchor_mandatory = (
        planned_mandatory_map(user_id, anchor_tomorrow, anchor_horizon_end)
        if anchor_tomorrow <= anchor_horizon_end else {}
    )
    # income_map also returns recorded manual transactions.  For an anchor
    # before today, today's card income would otherwise appear once in the
    # opening balance/allocation and once again as forecast income.  Remove it
    # so the previous buffer remains untouched.
    for income_day, recorded in recorded_income_map(user_id, period_anchor, today).items():
        if income_day not in anchor_income:
            continue
        corrected = round(anchor_income[income_day] - recorded, 2)
        if corrected:
            anchor_income[income_day] = corrected
        else:
            anchor_income.pop(income_day)
    anchor_plan = calculate_cashflow_plan(
        today=period_anchor,
        horizon_end=anchor_horizon_end,
        opening_balance_before_today_spend=anchor_opening,
        spent_today=0,
        income_by_date=anchor_income,
        mandatory_by_date=anchor_mandatory,
    )
    period_days = (period.end - period_anchor).days + 1
    period_budget = round(anchor_plan.today_target * period_days + daily_income_period, 2)
    remaining_period = round(
        max(0.0, period_budget - spent_period - transferred_period), 2
    )
    # Spread the card money that existed before today's purchases across all
    # remaining days.  This is the carry-over limit shown to the user.  The
    # forecast plan may produce a larger number after midnight because it sees
    # the already-reduced cash balance; exposing that number would promise more
    # money than is actually left on the card.
    today_card_before_spend = round(remaining_period + spent_today, 2)
    carried_daily = amount(cents(today_card_before_spend) // days_left)
    carried_available_today = round(carried_daily - spent_today, 2)
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
        daily_target=current_plan.daily_target,
        today_target=current_plan.today_target,
        spent_today=spent_today,
        income_overrides=cashflow_income_overrides(user_id, tomorrow, horizon_end)
        if tomorrow <= horizon_end
        else {},
    )

    # "В буфере сейчас" is money that can stay on the separate buffer
    # account after the whole current pay period has been funded.  The
    # calculation in CashflowPlan only subtracts today's allowance, which is
    # useful for the daily-limit screen but overstates the balance that may be
    # moved to the buffer account.  The first period row already reserves the
    # daily budget for every remaining day of the current period and excludes
    # piggy-bank movements.
    buffer_balance_now = round(max(0.0, available_cash - remaining_period), 2)
    if periods:
        # The current-period row must use the same carried limit and protected
        # buffer as the home screen.  Future rows remain forecast values.
        periods[0]["daily"] = carried_daily
        periods[0]["buffer"] = buffer_balance_now
        periods[0]["put_aside"] = buffer_balance_now
        periods[0]["take"] = 0.0

    # Income explicitly sent to the buffer is kept out of the card's daily
    # allowance and is added to every reserve balance in the current forecast.
    manual_buffer = manual_buffer_income(user_id, start_date, today)
    if manual_buffer:
        buffer_balance_now = round(buffer_balance_now + manual_buffer, 2)
        for row in periods:
            row["buffer"] = round(row["buffer"] + manual_buffer, 2)
        if periods:
            periods[0]["put_aside"] = round(periods[0]["put_aside"] + manual_buffer, 2)

    return {
        "enabled": True,
        "settings": settings,
        "today": today.isoformat(),
        "horizon_end": horizon_end.isoformat(),
        "current_cash": current_cash,
        "available_cash": available_cash,
        "reserved_mandatory": reserved_mandatory,
        "mandatory_period": mandatory_period,
        "piggy_bank_balance": piggy_bank_balance(user_id),
        "daily_target": carried_daily,
        "today_target": carried_daily,
        "available_today": carried_available_today,
        "buffer_balance": buffer_balance_now,
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
            "UPDATE settings SET cashflow_enabled=?,cashflow_start_date=?,cashflow_start_capital=?,"
            "initial_vacation_reserve=0,updated_at=CURRENT_TIMESTAMP "
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
        "settings": snapshot["settings"],
        "today": snapshot["today"],
        "horizon_end": snapshot["horizon_end"],
        "daily_target": snapshot["daily_target"],
        "available_today": snapshot["available_today"],
        "buffer_balance": snapshot["buffer_balance"],
        "capital_shortfall": snapshot["capital_shortfall"],
        "periods": snapshot["periods"],
    }


def editable_cashflow_period(user_id: int, period_start: date) -> bool:
    today = clock.today()
    settings = cashflow_settings(user_id)
    horizon_end = add_months(today, max(1, min(12, int(settings["forecast_months"]))))
    return any(
        period["date"] == period_start
        for period in cashflow_periods(user_id, today, horizon_end)[1:]
    )


@app.put("/api/cashflow/income-overrides/{period_start}")
def save_cashflow_income_override(
    period_start: date,
    payload: CashflowIncomeOverrideIn,
    user: TelegramUser = Depends(current_user),
) -> dict:
    uid = legacy.user_ready(user)
    if not editable_cashflow_period(uid, period_start):
        raise HTTPException(422, "Можно изменить только будущую выплату из текущего прогноза")
    with legacy.connect() as con:
        con.execute(
            "INSERT INTO cashflow_income_overrides(user_id,period_start,amount) VALUES(?,?,?) "
            "ON CONFLICT(user_id,period_start) DO UPDATE SET "
            "amount=excluded.amount,updated_at=CURRENT_TIMESTAMP",
            (uid, period_start.isoformat(), payload.amount),
        )
    legacy.invalidate_current_auto_reserve(uid)
    return get_buffer(user)


@app.delete("/api/cashflow/income-overrides/{period_start}")
def delete_cashflow_income_override(
    period_start: date, user: TelegramUser = Depends(current_user)
) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        deleted = con.execute(
            "DELETE FROM cashflow_income_overrides WHERE user_id=? AND period_start IN (?,?)",
            (uid, period_start.isoformat(), (period_start - timedelta(days=1)).isoformat()),
        )
    if deleted.rowcount == 0:
        raise HTTPException(404, "Корректировка выплаты не найдена")
    legacy.invalidate_current_auto_reserve(uid)
    return get_buffer(user)


@app.get("/api/piggy-bank")
def get_piggy_bank(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    return piggy_bank_snapshot(uid)


def add_piggy_bank_movement(
    user_id: int, direction: Literal["deposit", "withdraw"], payload: PiggyBankMovementIn
) -> dict:
    if payload.movement_date > clock.today():
        raise HTTPException(422, "Дата операции не может быть в будущем")
    if direction == "withdraw" and payload.source == "daily_budget":
        raise HTTPException(422, "Возврат в дневной бюджет пока выполняется отдельной операцией")
    if direction == "deposit" and payload.source == "daily_budget":
        flow = cashflow_snapshot(user_id)
        available = float(flow.get("available_today", 0)) if flow.get("enabled") else 0.0
        if payload.amount > max(0.0, available):
            raise HTTPException(422, "Нельзя перенести больше неизрасходованного остатка за день")
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        movement_id = con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source) VALUES(?,?,?,?,?,?)",
            (user_id, direction, payload.amount, payload.movement_date.isoformat(), payload.note, payload.source),
        ).lastrowid
        try:
            check_piggy_history(con, user_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    legacy.invalidate_current_auto_reserve(user_id)
    return legacy.one(
        "SELECT id,direction,amount,movement_date,note,source,created_at "
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
