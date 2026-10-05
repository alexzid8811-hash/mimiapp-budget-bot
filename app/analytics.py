"""Spending analytics: where the money went over a month, card period or year.

The numbers are plain sums of recorded operations, so the page and the Excel
export always agree with the operations list.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from . import clock, planning
from .auth import TelegramUser, current_user
from .budget import add_months
from .db import connect, ensure_user
from .money import amount, cents


router = APIRouter(prefix="/api", tags=["analytics"])
Mode = Literal["month", "period", "year"]

MONTHS_NOMINATIVE = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)
MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
NO_CATEGORY = "Без категории"
TOP_OPERATIONS = 3


def _short(day: date) -> str:
    return f"{day.day} {MONTHS_GENITIVE[day.month - 1]}"


def range_for(uid: int, mode: Mode, anchor: date) -> tuple[date, date, str]:
    """Calendar range containing ``anchor`` and its human label."""
    if mode == "year":
        return date(anchor.year, 1, 1), date(anchor.year, 12, 31), f"{anchor.year} год"
    if mode == "period":
        try:
            period = planning.user_period(uid, anchor)
        except ValueError as exc:
            raise ValueError("Период карты недоступен: задайте зарплату и аванс в настройках") from exc
        return period.start, period.end, f"{_short(period.start)} — {_short(period.end)}"
    start = anchor.replace(day=1)
    end = add_months(start, 1) - timedelta(days=1)
    return start, end, f"{MONTHS_NOMINATIVE[start.month - 1]} {start.year}"


def _expenses(con, uid: int, start: date, end: date) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT t.id,t.amount,t.tx_date,t.note,t.category_id,t.bill_rule_id,"
        "c.title AS category_title,c.emoji AS category_emoji,c.sort_order AS category_order "
        "FROM transactions t LEFT JOIN categories c ON c.id=t.category_id AND c.user_id=t.user_id "
        "WHERE t.user_id=? AND t.type='expense' AND t.tx_date BETWEEN ? AND ? ORDER BY t.tx_date,t.id",
        (uid, start.isoformat(), end.isoformat()),
    ).fetchall()]


def _piggy_deposits(con, uid: int, start: date, end: date) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT amount,movement_date FROM piggy_bank_movements "
        "WHERE user_id=? AND direction='deposit' AND movement_date BETWEEN ? AND ?",
        (uid, start.isoformat(), end.isoformat()),
    ).fetchall()]


def _kinds(expenses: list[dict], deposits: list[dict]) -> dict:
    daily = sum(cents(e["amount"]) for e in expenses if e["bill_rule_id"] is None)
    bills = sum(cents(e["amount"]) for e in expenses if e["bill_rule_id"] is not None)
    piggy = sum(cents(d["amount"]) for d in deposits)
    return {
        "daily": amount(daily), "bills": amount(bills), "piggy": amount(piggy),
        "total": amount(daily + bills + piggy),
    }


def _category_key(e: dict):
    return e["category_id"] if e["category_title"] is not None else None


def _categories(expenses: list[dict], previous: list[dict]) -> list[dict]:
    groups: dict = {}
    for e in expenses:
        if e["bill_rule_id"] is not None:
            continue
        key = _category_key(e)
        group = groups.setdefault(key, {
            "id": key,
            "title": e["category_title"] or NO_CATEGORY,
            "emoji": e["category_emoji"] or "💳",
            "order": e["category_order"] if key is not None else 10**6,
            "cents": 0,
            "operations": [],
        })
        group["cents"] += cents(e["amount"])
        group["operations"].append(e)
    prev_totals: dict = defaultdict(int)
    for e in previous:
        if e["bill_rule_id"] is None:
            prev_totals[_category_key(e)] += cents(e["amount"])
    total = sum(g["cents"] for g in groups.values())
    result = []
    for g in sorted(groups.values(), key=lambda g: (-g["cents"], g["order"], g["title"])):
        top = sorted(g["operations"], key=lambda e: (-cents(e["amount"]), e["tx_date"], e["id"]))[:TOP_OPERATIONS]
        result.append({
            "id": g["id"],
            "title": g["title"],
            "emoji": g["emoji"],
            "amount": amount(g["cents"]),
            "previous": amount(prev_totals.get(g["id"], 0)),
            "share": round(g["cents"] / total, 4) if total else 0.0,
            "count": len(g["operations"]),
            "top": [
                {"date": e["tx_date"], "amount": amount(cents(e["amount"])), "note": e["note"] or ""}
                for e in top
            ],
        })
    return result


def _days(expenses: list[dict], start: date, end: date) -> list[dict]:
    totals: dict[str, int] = defaultdict(int)
    for e in expenses:
        if e["bill_rule_id"] is None:
            totals[e["tx_date"]] += cents(e["amount"])
    days, day = [], start
    while day <= end:
        days.append({"date": day.isoformat(), "amount": amount(totals.get(day.isoformat(), 0))})
        day += timedelta(days=1)
    return days


def monthly(uid: int, first_month: date, count: int) -> list[dict]:
    """Per calendar month: everyday spending, bills and piggy-bank deposits."""
    first_month = first_month.replace(day=1)
    end = add_months(first_month, count) - timedelta(days=1)
    with connect() as con:
        expenses = _expenses(con, uid, first_month, end)
        deposits = _piggy_deposits(con, uid, first_month, end)
    by_month: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in expenses:
        by_month[e["tx_date"][:7]]["bills" if e["bill_rule_id"] is not None else "daily"] += cents(e["amount"])
    for d in deposits:
        by_month[d["movement_date"][:7]]["piggy"] += cents(d["amount"])
    rows = []
    for index in range(count):
        month = add_months(first_month, index)
        key = month.isoformat()[:7]
        values = by_month.get(key, {})
        rows.append({
            "month": key,
            "label": MONTHS_NOMINATIVE[month.month - 1],
            "daily": amount(values.get("daily", 0)),
            "bills": amount(values.get("bills", 0)),
            "piggy": amount(values.get("piggy", 0)),
        })
    return rows


def _open_since(uid: int, today: date) -> date:
    """Unpaid bills before the current card period are history, not plans."""
    try:
        return planning.user_period(uid, today).start
    except ValueError:
        return today.replace(day=1)


def _bills(uid: int, start: date, end: date, today: date) -> list[dict]:
    open_since = _open_since(uid, today).isoformat()
    groups: dict = {}
    for event in planning.bill_events(uid, start, end):
        if not event.get("paid") and event["due_date"] < open_since:
            continue
        group = groups.setdefault(event["id"], {
            "title": event.get("title") or "Платёж", "paid": 0, "due": 0,
            "count": 0, "paid_count": 0, "next_due": None, "next_amount": 0,
        })
        group["count"] += 1
        if event.get("paid"):
            group["paid"] += cents(event["amount"])
            group["paid_count"] += 1
        else:
            group["due"] += cents(event["amount"])
            # Events come sorted by date, so the first unpaid one is the nearest.
            if group["next_due"] is None:
                group["next_due"] = event["due_date"]
                group["next_amount"] = cents(event["amount"])
    rows = [
        {
            "title": g["title"], "paid_amount": amount(g["paid"]), "due_amount": amount(g["due"]),
            "count": g["count"], "paid_count": g["paid_count"], "next_due": g["next_due"],
            "next_amount": amount(g["next_amount"]),
        }
        for g in sorted(groups.values(), key=lambda g: -(g["paid"] + g["due"]))
    ]
    return rows + _upcoming_bills(uid, start, end, today, set(groups))


def _upcoming_bills(uid: int, start: date, end: date, today: date, shown: set) -> list[dict]:
    """Active bills with no payment in the current range, e.g. one added after
    its day of month had passed: it starts next month, but the user should
    still see it. These rows carry no amount, so period totals don't change."""
    if not start <= today <= end:
        return []
    # Only bills that are new: a regular bill whose day just falls outside a
    # card period already had payments before it and is not "first".
    shown = shown | {e["id"] for e in planning.bill_events(uid, start - timedelta(days=62), start - timedelta(days=1))}
    first: dict = {}
    for event in planning.bill_events(uid, end + timedelta(days=1), end + timedelta(days=62)):
        if event["id"] not in shown and not event.get("paid") and event.get("active", True):
            first.setdefault(event["id"], event)
    return [
        {
            "title": e.get("title") or "Платёж", "paid_amount": 0.0, "due_amount": 0.0,
            "count": 0, "paid_count": 0, "next_due": e["due_date"], "next_amount": amount(cents(e["amount"])),
        }
        for e in sorted(first.values(), key=lambda e: e["due_date"])
    ]


def _daily_limit(uid: int, start: date, end: date, today: date) -> float | None:
    """Planned per-day card budget, known only for the current card period."""
    from . import engine  # local import: the engine is heavy and optional here

    try:
        flow = engine.compute(uid)
    except Exception:  # noqa: BLE001 - the chart works without the line
        return None
    period = flow.get("period") or {}
    if not flow.get("enabled"):
        return None
    if period.get("start") != start.isoformat() or not (start <= today <= end):
        return None
    days = (end - start).days + 1
    budget = cents(flow.get("period_budget") or 0)
    return amount(budget // days) if budget > 0 and days > 0 else None


def summary(uid: int, mode: Mode, anchor: date, today: date | None = None) -> dict:
    today = today or clock.today()
    start, end, label = range_for(uid, mode, anchor)
    prev_start, prev_end, prev_label = range_for(uid, mode, start - timedelta(days=1))
    # A range still in progress is compared with the same number of days of
    # the previous one, otherwise every current month looks cheaper.
    compare_end = prev_end
    if start <= today < end:
        compare_end = min(prev_end, prev_start + (today - start))
    with connect() as con:
        expenses = _expenses(con, uid, start, end)
        deposits = _piggy_deposits(con, uid, start, end)
        prev_expenses = _expenses(con, uid, prev_start, compare_end)
        prev_deposits = _piggy_deposits(con, uid, prev_start, compare_end)
    kinds = _kinds(expenses, deposits)
    previous = _kinds(prev_expenses, prev_deposits)
    if mode == "year":
        months = monthly(uid, start, 12)
    else:
        months = monthly(uid, add_months(end.replace(day=1), -5), 6)
    return {
        "mode": mode,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "label": label,
        "previous_label": prev_label,
        "previous_anchor": prev_start.isoformat(),
        "previous_partial": compare_end < prev_end,
        "next_anchor": (end + timedelta(days=1)).isoformat() if end < today else None,
        "today": today.isoformat(),
        "totals": kinds,
        "previous_totals": previous,
        "categories": _categories(expenses, prev_expenses),
        "days": [] if mode == "year" else _days(expenses, start, end),
        "daily_limit": _daily_limit(uid, start, end, today) if mode == "period" else None,
        "months": months,
        "bills": _bills(uid, start, end, today),
    }


@router.get("/analytics")
def analytics(
    mode: Mode = "month",
    anchor: date | None = None,
    user: TelegramUser = Depends(current_user),
) -> dict:
    ensure_user(user.id, user.first_name, user.username)
    today = clock.today()
    try:
        return summary(user.id, mode, min(anchor or today, today), today)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
