"""Reads a user's data, runs :mod:`app.ledger` and builds the API snapshot."""
from __future__ import annotations

from datetime import date, timedelta

from . import clock, planning
from .budget import add_months
from .db import connect
from .ledger import LedgerInput, buffer_rows, run_ledger
from .money import amount, cents

MARGIN = timedelta(days=62)


def settings_for(uid: int) -> dict:
    with connect() as con:
        row = con.execute(
            "SELECT cashflow_enabled,cashflow_start_date,cashflow_start_capital,forecast_months "
            "FROM settings WHERE user_id=?",
            (uid,),
        ).fetchone()
    data = dict(row) if row else {}
    return {
        "cashflow_enabled": int(data.get("cashflow_enabled") or 0),
        "start_date": data.get("cashflow_start_date") or clock.today().isoformat(),
        "start_capital": float(data.get("cashflow_start_capital") or 0),
        "forecast_months": int(data.get("forecast_months") or 4),
    }


def _add(target: dict[date, int], day: date, value: int) -> None:
    if value:
        target[day] = target.get(day, 0) + value


def payday_schedule(uid: int, start: date, end: date) -> dict:
    """Actual paydays with forecast amounts and titles in ``[start, end]``."""
    paydays: dict[date, dict] = {}
    other: list[dict] = []
    for event in planning.income_events(uid, start, end):
        if event["is_payday"]:
            item = paydays.setdefault(event["date"], {"amount": 0, "titles": []})
            item["amount"] += cents(event["amount"])
            if event.get("title") and event["title"] not in item["titles"]:
                item["titles"].append(event["title"])
        else:
            other.append(event)
    return {"paydays": paydays, "other": other}


def income_overrides(uid: int, paydays: set[date]) -> dict[date, int]:
    """Actual payday amounts by payday.

    Rows are keyed by the first day of the card period (payday + 1).  Very old
    backups stored the payday itself; both forms are accepted.
    """
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT period_start,amount FROM cashflow_income_overrides WHERE user_id=?", (uid,)
        )]
    result: dict[date, int] = {}
    for row in sorted(rows, key=lambda r: r["period_start"]):
        stored = date.fromisoformat(row["period_start"])
        if stored - timedelta(days=1) in paydays:
            result[stored - timedelta(days=1)] = cents(row["amount"])
        elif stored in paydays and stored not in result:
            result[stored] = cents(row["amount"])
    return result


def build_input(uid: int, *, start: date, capital: float, months: int, today: date) -> tuple[LedgerInput, dict]:
    horizon = add_months(today, months)
    data_end = add_months(horizon, months)
    schedule = payday_schedule(uid, start - MARGIN, data_end + MARGIN)
    paydays = sorted(schedule["paydays"])
    overrides = income_overrides(uid, set(paydays))

    buffer_in: dict[date, int] = {}
    payday_amount: dict[date, int] = {}
    for day, item in schedule["paydays"].items():
        value = overrides.get(day, item["amount"])
        payday_amount[day] = value
        if start <= day <= data_end:
            _add(buffer_in, day, value)
    # Non-payday scheduled income (vacation pay, "other" income rules) is a
    # forecast like salary/advance: once its date is today or in the past, it
    # only counts once an actual transaction records the money as received.
    for event in schedule["other"]:
        if today < event["date"] <= data_end:
            _add(buffer_in, event["date"], cents(event["amount"]))

    card_in: dict[date, int] = {}
    card_cover: dict[date, int] = {}
    card_out: dict[date, int] = {}
    card_to_piggy: dict[date, int] = {}
    bills_out: dict[date, int] = {}
    reserved = 0
    window = (uid, start.isoformat(), today.isoformat())
    with connect() as con:
        for row in con.execute(
            "SELECT type,amount,tx_date,COALESCE(income_destination,'daily') AS destination "
            "FROM transactions WHERE user_id=? AND bill_rule_id IS NULL AND tx_date BETWEEN ? AND ?",
            window,
        ):
            day = date.fromisoformat(row["tx_date"])
            value = cents(row["amount"])
            if row["type"] == "expense":
                _add(card_out, day, value)
            elif row["destination"] == "buffer":
                _add(buffer_in, day, value)
            elif row["destination"] == "daily":
                _add(card_in, day, value)
        for row in con.execute(
            "SELECT t.amount,t.tx_date,t.bill_due_date,t.bill_planned_amount,"
            "COALESCE(p.amount,0) AS to_piggy FROM transactions t "
            "LEFT JOIN piggy_bank_movements p ON p.user_id=t.user_id AND p.bill_payment_id=t.id "
            "WHERE t.user_id=? AND t.type='expense' AND t.bill_rule_id IS NOT NULL "
            "AND t.tx_date BETWEEN ? AND ?",
            window,
        ):
            paid_on = date.fromisoformat(row["tx_date"])
            due = date.fromisoformat(row["bill_due_date"]) if row["bill_due_date"] else paid_on
            actual = cents(row["amount"])
            planned = cents(row["bill_planned_amount"]) if row["bill_planned_amount"] is not None else actual
            # The buffer releases exactly the reserved amount.  The difference
            # belongs to the card: a remainder returns to the daily budget (or
            # to the piggy bank by the user's choice), an overpayment is paid
            # from the card and never from the buffer.
            _add(bills_out, max(start, min(due, paid_on)), planned)
            difference = planned - actual - cents(row["to_piggy"])
            if difference > 0:
                _add(card_in, paid_on, difference)
            elif difference < 0:
                _add(card_out, paid_on, -difference)
        for row in con.execute(
            "SELECT direction,amount,movement_date,COALESCE(purpose,'') AS purpose "
            "FROM piggy_bank_movements WHERE user_id=? AND source='daily_budget' "
            "AND bill_payment_id IS NULL AND movement_date BETWEEN ? AND ?",
            window,
        ):
            day = date.fromisoformat(row["movement_date"])
            value = cents(row["amount"])
            if row["direction"] == "deposit":
                _add(card_to_piggy, day, value)
            elif row["purpose"] == "cover_overspend":
                _add(card_cover, day, value)
            else:
                _add(card_in, day, value)
        stored = {
            date.fromisoformat(r["period_start"]): cents(r["amount"])
            for r in con.execute(
                "SELECT period_start,amount FROM card_allocations WHERE user_id=?", (uid,)
            )
        }

    unpaid: dict[date, int] = {}
    for event in planning.bill_events(uid, start, data_end):
        if event.get("paid"):
            continue
        due = date.fromisoformat(event["due_date"])
        value = cents(event["amount"])
        _add(bills_out, due, value)
        _add(unpaid, due, value)
        if due <= today:
            reserved += value

    inp = LedgerInput(
        start=start, today=today, horizon=horizon, data_end=data_end, months=months,
        start_capital=cents(capital), paydays=paydays, buffer_in=buffer_in,
        bills_out=bills_out, card_in=card_in, card_cover=card_cover, card_out=card_out,
        card_to_piggy=card_to_piggy, stored_allocations=stored,
    )
    extras = {
        "titles": {day: ", ".join(item["titles"]) or "Выплата" for day, item in schedule["paydays"].items()},
        "planned_payday": {day: item["amount"] for day, item in schedule["paydays"].items()},
        "payday_amount": payday_amount,
        "overridden": set(overrides),
        "reserved": reserved,
        "unpaid": unpaid,
    }
    return inp, extras


def store_allocations(uid: int, to_store: dict) -> None:
    if not to_store:
        return
    with connect() as con:
        con.executemany(
            "INSERT INTO card_allocations(user_id,period_start,funded_on,amount) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,period_start) DO UPDATE SET funded_on=excluded.funded_on,"
            "amount=excluded.amount,updated_at=CURRENT_TIMESTAMP",
            [(uid, start.isoformat(), funded.isoformat(), amount(value))
             for start, (funded, value) in to_store.items()],
        )


def forget_allocations(uid: int, from_day: date | None = None) -> None:
    """Drop stored allocations so they are recalculated (history corrections)."""
    with connect() as con:
        if from_day is None:
            con.execute("DELETE FROM card_allocations WHERE user_id=?", (uid,))
        else:
            con.execute(
                "DELETE FROM card_allocations WHERE user_id=? AND funded_on>=?",
                (uid, from_day.isoformat()),
            )


def piggy_balance(uid: int) -> float:
    with connect() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction='deposit' THEN amount ELSE -amount END),0) "
            "FROM piggy_bank_movements WHERE user_id=?",
            (uid,),
        ).fetchone()
    return round(float(row[0]), 2)


def _period_dict(period) -> dict:
    return {"start": period.start.isoformat(), "end": period.end.isoformat(), "days": period.days}


def compute(uid: int, *, force_enabled: bool = False) -> dict:
    """Full budget snapshot.  With the start capital switched off, the budget
    is calculated from the latest payday with an empty account."""
    settings = settings_for(uid)
    today = clock.today()
    months = max(1, min(12, settings["forecast_months"]))
    enabled = bool(settings["cashflow_enabled"]) or force_enabled
    if enabled:
        start = min(date.fromisoformat(settings["start_date"]), today)
        capital = settings["start_capital"]
    else:
        recent = [d for d in payday_schedule(uid, today - MARGIN, today)["paydays"] if d <= today]
        start, capital = (max(recent) if recent else today), 0.0

    inp, extras = build_input(uid, start=start, capital=capital, months=months, today=today)
    result = run_ledger(inp)
    if enabled:
        store_allocations(uid, result.to_store)

    current = result.current
    days_left = (current.end - today).days + 1
    next_forecast = next(
        (f for s, f in sorted(result.forecasts.items()) if s > current.start), None
    )
    pending = sum(
        result.allocations[p.start] for p in result.periods
        if p.funded_on <= today < p.start
    )
    buffer_now = result.buffer_end[today]
    reserved = extras["reserved"]
    card_now = result.card_end_today

    with connect() as con:
        spent_period = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type='expense' "
            "AND bill_rule_id IS NULL AND tx_date BETWEEN ? AND ?",
            (uid, max(current.start, start).isoformat(), today.isoformat()),
        ).fetchone()[0]
    # Obligations of the current card period that are still to be paid.
    mandatory_period = sum(
        v for d, v in extras["unpaid"].items() if max(current.start, start) <= d <= current.end
    )

    rows = []
    for index, row in enumerate(buffer_rows(inp, result)):
        funded = row.funded
        payday = row.payday
        card_start = funded.period.start if funded else None
        rows.append({
            "start": row.start.isoformat(),
            "end": row.end.isoformat(),
            "days": (row.end - row.start).days + 1,
            "kind": extras["titles"].get(payday, "Старт") if payday else "Старт",
            "payday": payday.isoformat() if payday else None,
            "received": amount(row.received),
            "payday_amount": amount(extras["payday_amount"].get(payday, 0)) if payday else 0.0,
            "planned_payday_amount": amount(extras["planned_payday"].get(payday, 0)) if payday else 0.0,
            "income_overridden": bool(payday and payday in extras["overridden"]),
            # The actual salary can be entered for the current and future
            # card periods; closed periods stay as they were.
            "received_editable": bool(payday and payday >= start and funded and funded.period.end >= today),
            "override_key": card_start.isoformat() if card_start and payday else None,
            "mandatory": amount(row.mandatory),
            "to_card": amount(row.to_card),
            "free": amount(row.received - row.mandatory),
            "card_period": _period_dict(funded.period) if funded else None,
            "daily": amount(funded.daily) if funded else 0.0,
            "buffer_start": amount(row.buffer_start),
            "buffer": amount(row.buffer_end),
            "put_aside": amount(max(0, row.buffer_end - row.buffer_start)),
            "take": amount(max(0, row.buffer_start - row.buffer_end)),
            "shortfall": amount(max(0, -row.lowest)),
            "current": index == 0,
        })

    next_income = next(
        ({"date": d.isoformat(), "income": amount(v)}
         for d, v in sorted(inp.buffer_in.items()) if d > today and v > 0),
        None,
    )
    if result.shortfall > 0:
        reason = (
            f"Буфер не покрывает обязательные платежи: к {result.shortfall_date:%d.%m.%Y} "
            f"не хватает {amount(result.shortfall):.2f}. Дневной лимит новых периодов снижен до нуля."
        )
    else:
        reason = (
            "Буфер держит обязательные платежи на их даты и деньги для будущих слабых периодов. "
            "Он не входит в «можно сегодня» и не покрывает перерасход."
        )

    return {
        "enabled": bool(settings["cashflow_enabled"]),
        "settings": settings,
        "today": today.isoformat(),
        "start_date": start.isoformat(),
        "horizon_end": inp.horizon.isoformat(),
        "period": {
            **_period_dict(current),
            "days_left": days_left,
            "is_last_day": days_left == 1,
            "payday": current.end.isoformat(),
            "payday_kind": extras["titles"].get(current.end, "Выплата"),
        },
        "next_period": {
            **_period_dict(next_forecast.period),
            "allocation": amount(next_forecast.allocation),
            "daily": amount(next_forecast.daily),
        } if next_forecast else None,
        "card_balance": amount(card_now),
        "card_start_today": amount(result.card_start_today),
        "today_target": amount(result.today_limit),
        "daily_target": amount(result.today_limit),
        "available_today": amount(result.available_today),
        "spent_today": amount(result.spent_today),
        "overspend": amount(result.overspend),
        "tomorrow_limit": amount(result.tomorrow_limit),
        "buffer_balance": amount(buffer_now),
        "pending_card_allocation": amount(pending),
        "reserved_mandatory": amount(reserved),
        "current_cash": amount(card_now + buffer_now + pending + reserved),
        "available_cash": amount(card_now + buffer_now + pending),
        "piggy_bank_balance": piggy_balance(uid),
        "period_budget": amount(result.forecasts[current.start].opening),
        "remaining_period": amount(card_now),
        "spent_period": round(float(spent_period), 2),
        "mandatory_period": amount(mandatory_period),
        "capital_shortfall": amount(result.shortfall),
        "shortfall_date": result.shortfall_date.isoformat() if result.shortfall_date else None,
        "next_income": next_income,
        "reason": reason,
        "periods": rows,
    }
