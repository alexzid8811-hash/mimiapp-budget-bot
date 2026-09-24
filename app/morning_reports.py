"""Data for a once-a-day Telegram summary of the personal budget."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta

from . import engine, planning
from .db import connect


def _next_rule_due_date(day_of_month: int, today: date) -> date:
    """Return the next calendar occurrence of a monthly bill rule."""
    year, month = today.year, today.month
    for _ in range(13):
        due = date(year, month, min(day_of_month, monthrange(year, month)[1]))
        if due >= today:
            return due
        month += 1
        if month == 13:
            year, month = year + 1, 1
    raise RuntimeError("Unable to calculate the next bill date")


def _nearest_unpaid_bills(user_id: int, today: date) -> tuple[date | None, list[dict]]:
    """Return every unpaid active bill on the nearest payment date."""
    with connect() as con:
        rules = con.execute(
            "SELECT id,title,amount,day_of_month FROM bill_rules "
            "WHERE user_id=? AND active=1 AND archived=0",
            (user_id,),
        ).fetchall()
        candidates = []
        for rule in rules:
            due = _next_rule_due_date(int(rule["day_of_month"]), today)
            paid = con.execute(
                "SELECT 1 FROM transactions WHERE user_id=? AND bill_rule_id=? "
                "AND bill_due_date=? LIMIT 1",
                (user_id, int(rule["id"]), due.isoformat()),
            ).fetchone()
            if not paid:
                candidates.append({
                    "due_date": due.isoformat(),
                    "title": rule["title"],
                    "amount": float(rule["amount"]),
                })

    nearest_date = min((item["due_date"] for item in candidates), default=None)
    return nearest_date, [item for item in candidates if item["due_date"] == nearest_date]


def pending_morning_reports(now: datetime) -> list[dict]:
    today = now.date()
    yesterday = today - timedelta(days=1)
    current_time = now.strftime("%H:%M")
    with connect() as con:
        settings = [dict(row) for row in con.execute(
            "SELECT user_id,morning_report_time FROM settings"
        )]
        sent = {row[0] for row in con.execute(
            "SELECT user_id FROM morning_reports WHERE report_date=?", (today.isoformat(),)
        )}

    reports: list[dict] = []
    for setting in settings:
        user_id = setting["user_id"]
        if user_id in sent or current_time < setting["morning_report_time"]:
            continue
        with connect() as con:
            # Keep every operation separate. A grouped category can hide a
            # transaction after its category was edited or reordered.
            expenses = [dict(row) for row in con.execute(
                "SELECT t.id,COALESCE(NULLIF(t.note,''),c.title,'Расход') AS title,t.amount "
                "FROM transactions t LEFT JOIN categories c "
                "ON c.id=t.category_id AND c.user_id=t.user_id "
                "WHERE t.user_id=? AND t.type='expense' AND t.tx_date=? ORDER BY t.id",
                (user_id, yesterday.isoformat()),
            )]
            previous = con.execute(
                "SELECT daily_limit FROM morning_reports WHERE user_id=? "
                "AND report_date<? AND daily_limit IS NOT NULL "
                "ORDER BY report_date DESC LIMIT 1", (user_id, today.isoformat())
            ).fetchone()

        # The same calculation as the home screen: with the start capital
        # switched off it still runs from the latest payday.
        flow = engine.compute(user_id)
        period = planning.user_period(user_id, today)
        nearest_date, nearest = _nearest_unpaid_bills(user_id, today)
        daily_amount = float(flow.get("available_today", 0))
        daily_limit = float(flow.get("today_target", daily_amount))
        reports.append({
            "user_id": user_id,
            "report_date": today,
            "yesterday": yesterday,
            "expenses": expenses,
            "spent_total": round(sum(float(item["amount"]) for item in expenses), 2),
            "period_days_left": (period.end - today).days + 1,
            "period_remaining": float(flow.get("remaining_period", 0)),
            "card_balance": float(flow.get("current_cash", 0)),
            "daily_amount": daily_amount,
            "daily_limit": daily_limit,
            "daily_change": None if previous is None
                else round(daily_limit - float(previous["daily_limit"]), 2),
            "piggy_balance": float(flow.get("piggy_bank_balance", 0)),
            "buffer_balance": float(flow.get("buffer_balance", 0)),
            "nearest_due_date": nearest_date,
            "nearest_days_left": None if nearest_date is None
                else (datetime.fromisoformat(nearest_date).date() - today).days,
            "nearest_bills": nearest,
        })
    return reports


def record_morning_report(report: dict) -> None:
    with connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO morning_reports(user_id,report_date,daily_amount,daily_limit) VALUES(?,?,?,?)",
            (report["user_id"], report["report_date"].isoformat(), report["daily_amount"], report["daily_limit"]),
        )
