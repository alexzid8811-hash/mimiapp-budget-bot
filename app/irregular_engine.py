"""Reads a user's data, runs :mod:`app.irregular` and builds the API snapshot."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from . import clock, planning
from .db import connect
from .irregular import (
    Bill, Income, IrregularInput, IrregularResult, IrregularSettings, ReserveMove, Split, bills_horizon,
    replay,
)
from .money import amount, cents

SETTINGS_COLUMNS = (
    "budget_mode", "irregular_reserve_percent", "irregular_reserve_target", "irregular_stretch_days",
    "irregular_bills_lookahead_days", "irregular_bills_scope", "irregular_start_date", "irregular_start_total",
    "irregular_start_reserve",
)
PREVIEW_ID = 10 ** 12  # processed after every stored income of the same day
SPENDING_WINDOW = 30

KIND_TITLES = {
    "start": "Резерв на старте",
    "income": "Процент от дохода",
    "income_reserve": "Доход целиком в резерв",
    "deposit": "Пополнение",
    "from_free": "Возврат из свободных денег",
    "pay_expense": "Оплата расхода",
    "to_free": "В свободные деньги",
    "cover_overspend": "Покрытие перерасхода",
}


def settings_for(uid: int, con=None) -> dict:
    sql = f"SELECT {','.join(SETTINGS_COLUMNS)} FROM settings WHERE user_id=?"
    if con is None:
        with connect() as own:
            row = own.execute(sql, (uid,)).fetchone()
    else:
        row = con.execute(sql, (uid,)).fetchone()
    data = dict(row) if row else {}
    target = data.get("irregular_reserve_target")
    return {
        "budget_mode": data.get("budget_mode") or "payroll",
        "reserve_percent": float(data.get("irregular_reserve_percent") if data.get("irregular_reserve_percent") is not None else 10),
        "reserve_target": None if target is None else float(target),
        "stretch_days": int(data.get("irregular_stretch_days") or 14),
        "lookahead_days": int(data["irregular_bills_lookahead_days"]) if data.get("irregular_bills_lookahead_days") is not None else 30,
        "bills_scope": "days" if data.get("irregular_bills_scope") == "days" else "month",
        "start_date": data.get("irregular_start_date"),
        "start_total": float(data.get("irregular_start_total") or 0),
        "start_reserve": float(data.get("irregular_start_reserve") or 0),
    }


def is_irregular(uid: int, con=None) -> bool:
    return settings_for(uid, con)["budget_mode"] == "irregular"


def _rules(settings: dict) -> IrregularSettings:
    target = settings["reserve_target"]
    return IrregularSettings(
        reserve_percent=settings["reserve_percent"],
        reserve_target=None if target is None else cents(target),
        stretch_days=settings["stretch_days"],
        lookahead_days=settings["lookahead_days"],
        bills_scope=settings["bills_scope"],
    )


def _add(target: dict, day: date, value: int) -> None:
    if value:
        target[day] = target.get(day, 0) + value


def _destination(value: str | None) -> str:
    return value if value in ("piggy", "reserve") else "split"


def _incomes(con, uid: int, start: date, today: date) -> list[Income]:
    return [
        Income(
            id=int(r["id"]), day=date.fromisoformat(r["tx_date"]), amount=cents(r["amount"]),
            # An income without a stored percent (entered before this mode)
            # never follows the current setting.
            percent=float(r["reserve_percent"] or 0),
            destination=_destination(r["income_destination"]), source=r["income_source"] or "",
        )
        for r in con.execute(
            "SELECT id,tx_date,amount,reserve_percent,income_destination,income_source FROM transactions "
            "WHERE user_id=? AND type='income' AND tx_date BETWEEN ? AND ? ORDER BY tx_date,id",
            (uid, start.isoformat(), today.isoformat()),
        )
    ]


def _moves(con, uid: int, start: date, today: date) -> list[ReserveMove]:
    return [
        ReserveMove(
            id=int(r["id"]), day=date.fromisoformat(r["movement_date"]), direction=r["direction"],
            amount=cents(r["amount"]), kind=r["kind"], reason=r["reason"] or "",
            expense_id=r["expense_transaction_id"],
        )
        for r in con.execute(
            "SELECT id,direction,amount,movement_date,reason,kind,expense_transaction_id "
            "FROM emergency_reserve_movements WHERE user_id=? AND movement_date BETWEEN ? AND ? "
            "ORDER BY movement_date,id",
            (uid, start.isoformat(), today.isoformat()),
        )
    ]


def start_of(settings: dict, today: date) -> date | None:
    if not settings["start_date"]:
        return None
    return min(date.fromisoformat(settings["start_date"]), today)


def check_reserve(con, uid: int) -> None:
    """Raise ValueError if the reserve is negative on any date.

    The reserve depends only on the start, incomes and reserve operations, so
    the check reads them through ``con`` and sees uncommitted changes."""
    settings = settings_for(uid, con)
    today = clock.today()
    start = start_of(settings, today)
    if start is None:
        return
    result = replay(IrregularInput(
        start=start, today=today, start_total=cents(settings["start_total"]),
        start_reserve=cents(settings["start_reserve"]), settings=_rules(settings),
        incomes=_incomes(con, uid, start, today), reserve_moves=_moves(con, uid, start, today),
    ))
    if result.reserve_violation is not None:
        raise ValueError(
            f"Резерв ушёл бы в минус {result.reserve_violation:%d.%m.%Y}: "
            "на эту дату в нём недостаточно денег"
        )


def _bills(con, uid: int, start: date, today: date, rules: IrregularSettings) -> list[Bill]:
    horizon = bills_horizon(today, rules)
    occurrences: dict[tuple[int, date], Bill] = {}
    for event in planning.bill_events(uid, start, horizon):
        due = date.fromisoformat(event["due_date"])
        occurrences[(int(event["id"]), due)] = Bill(
            rule_id=int(event["id"]), due=due, title=event.get("title") or "Платёж",
            amount=cents(event.get("planned_amount") if event.get("paid") else event["amount"]),
        )
    # Payments since the start, and earlier ones of bills due since the start
    # (paid in advance: the occurrence must not be put aside for again).
    for r in con.execute(
        "SELECT t.id,t.bill_rule_id,t.bill_due_date,t.tx_date,t.amount,t.bill_planned_amount,t.note,"
        "COALESCE(p.amount,0) AS to_piggy FROM transactions t "
        "LEFT JOIN piggy_bank_movements p ON p.user_id=t.user_id AND p.bill_payment_id=t.id "
        "WHERE t.user_id=? AND t.type='expense' AND t.bill_rule_id IS NOT NULL "
        "AND t.tx_date<=? AND (t.tx_date>=? OR t.bill_due_date>=?)",
        (uid, today.isoformat(), start.isoformat(), start.isoformat()),
    ):
        paid_on = date.fromisoformat(r["tx_date"])
        due = date.fromisoformat(r["bill_due_date"]) if r["bill_due_date"] else paid_on
        key = (int(r["bill_rule_id"]), due)
        actual = cents(r["amount"])
        planned = cents(r["bill_planned_amount"]) if r["bill_planned_amount"] is not None else actual
        known = occurrences.get(key)
        occurrences[key] = Bill(
            rule_id=key[0], due=due, title=(known.title if known else r["note"]) or "Платёж",
            amount=known.amount if known else planned, paid_on=paid_on, actual=actual,
            to_piggy=cents(r["to_piggy"]), payment_id=int(r["id"]),
        )
    return list(occurrences.values())


def build_input(uid: int, today: date | None = None) -> tuple[IrregularInput, dict] | None:
    settings = settings_for(uid)
    today = today or clock.today()
    start = start_of(settings, today)
    if start is None:
        return None
    window = (uid, start.isoformat(), today.isoformat())
    spent: dict[date, int] = {}
    to_piggy: dict[date, int] = {}
    free_in: dict[date, int] = {}
    free_cover: dict[date, int] = {}
    with connect() as con:
        incomes = _incomes(con, uid, start, today)
        moves = _moves(con, uid, start, today)
        for r in con.execute(
            "SELECT t.amount,t.tx_date FROM transactions t WHERE t.user_id=? AND t.type='expense' "
            "AND t.bill_rule_id IS NULL AND t.tx_date BETWEEN ? AND ? AND NOT EXISTS ("
            "SELECT 1 FROM emergency_reserve_movements m WHERE m.user_id=t.user_id "
            "AND m.expense_transaction_id=t.id)",
            window,
        ):
            _add(spent, date.fromisoformat(r["tx_date"]), cents(r["amount"]))
        for r in con.execute(
            "SELECT direction,amount,movement_date,COALESCE(purpose,'') AS purpose "
            "FROM piggy_bank_movements WHERE user_id=? AND source='daily_budget' "
            "AND bill_payment_id IS NULL AND movement_date BETWEEN ? AND ?",
            window,
        ):
            day = date.fromisoformat(r["movement_date"])
            value = cents(r["amount"])
            if r["direction"] == "deposit":
                _add(to_piggy, day, value)
            elif r["purpose"] in ("today", "cover_overspend"):
                _add(free_cover, day, value)
            else:
                _add(free_in, day, value)
        bills = _bills(con, uid, start, today, _rules(settings))
    inp = IrregularInput(
        start=start, today=today, start_total=cents(settings["start_total"]),
        start_reserve=cents(settings["start_reserve"]), settings=_rules(settings),
        incomes=incomes, bills=bills, reserve_moves=moves, spent=spent, to_piggy=to_piggy,
        free_in=free_in, free_cover=free_cover,
    )
    return inp, settings


def run(uid: int, today: date | None = None) -> tuple[IrregularInput, IrregularResult, dict] | None:
    built = build_input(uid, today)
    if built is None:
        return None
    inp, settings = built
    return inp, replay(inp), settings


def _avg_daily_spending(uid: int, today: date) -> int | None:
    """Average everyday spending per day over the last 30 days (with the days
    before the very first expense left out).  None without any history."""
    window_start = today - timedelta(days=SPENDING_WINDOW - 1)
    with connect() as con:
        first = con.execute(
            "SELECT MIN(tx_date) FROM transactions WHERE user_id=? AND type='expense' AND bill_rule_id IS NULL",
            (uid,),
        ).fetchone()[0]
        if first is None:
            return None
        window_start = max(window_start, date.fromisoformat(first))
        total = con.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type='expense' "
            "AND bill_rule_id IS NULL AND tx_date BETWEEN ? AND ?",
            (uid, window_start.isoformat(), today.isoformat()),
        ).fetchone()[0]
    days = (today - window_start).days + 1
    value = cents(total) // days if days > 0 else 0
    return value if value > 0 else None


def piggy_balance(uid: int) -> float:
    with connect() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction='deposit' THEN amount ELSE -amount END),0) "
            "FROM piggy_bank_movements WHERE user_id=?",
            (uid,),
        ).fetchone()
    return round(float(row[0]), 2)


def split_dict(split: Split) -> dict:
    return {"reserve": amount(split.reserve), "bills": amount(split.bills),
            "free": amount(split.free), "piggy": amount(split.piggy)}


def _bill_rows(inp: IrregularInput, result: IrregularResult) -> list[dict]:
    horizon = bills_horizon(result.today, inp.settings)
    rows = []
    for bill in result.bills:
        reserved = result.bills_fund.get(bill.key, 0)
        if bill.paid_on is None and bill.due < inp.start:
            continue
        if bill.paid_on is not None and bill.paid_on < result.today - timedelta(days=31):
            continue
        if bill.paid_on is None and bill.due > horizon and not reserved:
            continue
        rows.append({
            "id": bill.rule_id, "title": bill.title, "due_date": bill.due.isoformat(),
            "amount": amount(bill.amount), "reserved": amount(reserved),
            "missing": amount(max(0, bill.amount - reserved)) if bill.paid_on is None else 0.0,
            "paid": bill.paid_on is not None, "payment_id": bill.payment_id,
            "paid_amount": amount(bill.actual) if bill.paid_on else None,
        })
    return rows


def snapshot(uid: int) -> dict:
    settings = settings_for(uid)
    base = {"enabled": settings["budget_mode"] == "irregular", "settings": settings}
    ran = run(uid)
    if ran is None:
        return {**base, "configured": False}
    inp, result, settings = ran
    today = result.today
    avg = _avg_daily_spending(uid, today)
    rows = _bill_rows(inp, result)
    unpaid = [row for row in rows if not row["paid"]]
    next_bill = min(unpaid, key=lambda row: (row["due_date"], row["id"])) if unpaid else None
    shortfall = result.shortfall
    return {
        **base,
        "configured": True,
        "today": today.isoformat(),
        "start_date": inp.start.isoformat(),
        "today_target": amount(result.today_limit + result.cover_today),
        "today_limit": amount(result.today_limit),
        "available_today": amount(result.available_today),
        "spent_today": amount(result.spent_today),
        "overspend": amount(result.overspend),
        "tomorrow_limit": amount(result.tomorrow_limit),
        "free_balance": amount(result.free_end_today),
        "free_start_today": amount(result.free_start_today),
        "stretch_until": result.stretch_until.isoformat(),
        "days_left": result.days_left,
        "window_extended": result.window_extended,
        "reserve_balance": amount(result.reserve),
        "avg_daily_spending": amount(avg) if avg else None,
        "reserve_days": result.reserve // avg if avg else None,
        "bills_reserved": amount(result.bills_total),
        "next_bill": next_bill,
        "bills_shortfall": {
            "amount": amount(shortfall.amount), "date": shortfall.due.isoformat(),
        } if shortfall else None,
        "last_income_date": result.last_income.isoformat() if result.last_income else None,
        "days_without_income": result.days_without_income,
        # Not window_extended: an income that added no free money (all of it
        # to the bills, the reserve or the piggy bank) leaves the window
        # extended, but it is still an income.
        "no_income_warning": result.days_without_income >= inp.settings.stretch_days,
        "piggy_bank_balance": piggy_balance(uid),
        "bills": rows,
    }


def reserve_snapshot(uid: int) -> dict:
    ran = run(uid)
    if ran is None:
        return {"balance": 0.0, "movements": []}
    inp, result, _settings = ran
    linked = {m.id: m for m in inp.reserve_moves}
    movements = []
    for entry in reversed(result.reserve_history):
        stored = entry.kind not in ("start", "income", "income_reserve")
        move = linked.get(entry.ref_id) if stored else None
        movements.append({
            "id": entry.ref_id if stored else None,
            "income_id": entry.ref_id if not stored and entry.kind != "start" else None,
            "kind": entry.kind,
            "title": KIND_TITLES.get(entry.kind, entry.kind),
            "date": entry.day.isoformat(),
            "amount": amount(entry.amount),
            "balance": amount(entry.balance),
            "reason": entry.reason,
            "expense_id": move.expense_id if move else None,
        })
    return {"balance": amount(result.reserve), "movements": movements}


def incomes(uid: int, limit: int = 300) -> list[dict]:
    ran = run(uid)
    splits = ran[1].splits if ran else {}
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id,tx_date,amount,note,reserve_percent,income_destination,income_source "
            "FROM transactions WHERE user_id=? AND type='income' ORDER BY tx_date DESC,id DESC LIMIT ?",
            (uid, limit),
        )]
    for row in rows:
        split = splits.get(row["id"])
        row["split"] = split_dict(split) if split else None
    return rows


def preview(uid: int, *, value: float, day: date, percent: float, destination: str,
            exclude_id: int | None = None) -> dict:
    """Exact split of a not yet saved income (or of an edited one)."""
    built = build_input(uid)
    if built is None:
        raise ValueError("Сначала укажите старт режима подработок")
    inp, _settings = built
    if day < inp.start:
        raise ValueError("Дата дохода раньше даты старта режима")
    if day > inp.today:
        raise ValueError("Дата дохода не может быть в будущем")
    others = [i for i in inp.incomes if i.id != exclude_id]
    # An edited income keeps its id and so its place among the incomes of its day.
    probe = Income(exclude_id or PREVIEW_ID, day, cents(value), percent, _destination(destination))
    result = replay(replace(inp, incomes=others + [probe]))
    split = result.splits[probe.id]
    return {
        **split_dict(split),
        "amount": amount(cents(value)),
        "percent": percent,
        "stretch_until": result.stretch_until.isoformat(),
        "today_limit": amount(result.today_limit),
        "summary": summary_text(cents(value), split, percent, destination),
    }


def money_text(value: int) -> str:
    rub = f"{value / 100:,.2f}".replace(",", " ").replace(".", ",")
    if rub.endswith(",00"):
        rub = rub[:-3]
    return f"{rub} ₽"


def summary_text(value: int, split: Split, percent: float, destination: str) -> str:
    head = f"Пришло {money_text(value)}"
    if destination == "piggy":
        return f"{head} → целиком в копилку"
    if destination == "reserve":
        return f"{head} → целиком в резерв"
    pct = f"{percent:g}".replace(".", ",")
    return (f"{head} → {money_text(split.reserve)} в резерв ({pct}%), "
            f"{money_text(split.bills)} на обязательные, {money_text(split.free)} свободных")
