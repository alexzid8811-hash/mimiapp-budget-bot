"""Data for a once-a-day Telegram summary of the personal budget."""
from __future__ import annotations

from datetime import datetime, timedelta

from . import planning
from .cashflow_app import cashflow_snapshot
from .db import connect


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
            expenses = [dict(row) for row in con.execute(
                "SELECT COALESCE(c.title,NULLIF(t.note,''),'Расход') AS title,"
                "SUM(t.amount) AS amount FROM transactions t "
                "LEFT JOIN categories c ON c.id=t.category_id AND c.user_id=t.user_id "
                "WHERE t.user_id=? AND t.type='expense' AND t.tx_date=? "
                "GROUP BY COALESCE(c.title,NULLIF(t.note,''),'Расход') ORDER BY amount DESC",
                (user_id, yesterday.isoformat()),
            )]
            previous = con.execute(
                "SELECT daily_amount FROM morning_reports WHERE user_id=? AND report_date<? "
                "ORDER BY report_date DESC LIMIT 1", (user_id, today.isoformat())
            ).fetchone()

        flow = cashflow_snapshot(user_id)
        period = planning.user_period(user_id, today)
        upcoming = [event for event in planning.bill_events(user_id, today, today + timedelta(days=366))
                    if not event.get("paid")]
        nearest_date = min((event["due_date"] for event in upcoming), default=None)
        nearest = [event for event in upcoming if event["due_date"] == nearest_date]
        daily_amount = float(flow.get("available_today", 0))
        reports.append({
            "user_id": user_id,
            "report_date": today,
            "yesterday": yesterday,
            "expenses": expenses,
            "spent_total": round(sum(float(item["amount"]) for item in expenses), 2),
            "period_days_left": (period.end - today).days + 1,
            "period_remaining": float(flow.get("remaining_period", 0)),
            "daily_amount": daily_amount,
            "daily_change": None if previous is None or previous["daily_amount"] is None
                else round(daily_amount - float(previous["daily_amount"]), 2),
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
            "INSERT OR IGNORE INTO morning_reports(user_id,report_date,daily_amount) VALUES(?,?,?)",
            (report["user_id"], report["report_date"].isoformat(), report["daily_amount"]),
        )
