"""Selecting and recording Telegram reminders for unpaid mandatory bills."""
from __future__ import annotations

from datetime import date, timedelta

from . import planning
from .db import connect


def pending_bill_reminders(today: date, days_ahead: int) -> list[dict]:
    """Return unpaid bills due today or soon that have not been sent yet.

    Including the interval (rather than only exactly ``days_ahead``) makes a
    restart safe: a bill due in two days still gets a useful reminder if the
    bot was unavailable three days before it.
    """
    horizon = today + timedelta(days=max(0, days_ahead))
    with connect() as con:
        user_ids = [row[0] for row in con.execute("SELECT id FROM users")]
        already_sent = {
            (row[0], row[1], row[2])
            for row in con.execute(
                "SELECT user_id,bill_rule_id,due_date FROM bill_reminders "
                "WHERE due_date BETWEEN ? AND ?",
                (today.isoformat(), horizon.isoformat()),
            )
        }

    result: list[dict] = []
    for user_id in user_ids:
        for bill in planning.bill_events(user_id, today, horizon):
            key = (user_id, bill["id"], bill["due_date"])
            if bill.get("paid") or key in already_sent:
                continue
            due_date = date.fromisoformat(bill["due_date"])
            result.append({
                "user_id": user_id,
                "bill_rule_id": bill["id"],
                "title": bill["title"],
                "amount": float(bill["amount"]),
                "due_date": due_date,
                "days_left": (due_date - today).days,
            })
    return sorted(result, key=lambda item: (item["due_date"], item["user_id"], item["bill_rule_id"]))


def record_bill_reminder(reminder: dict) -> None:
    """Mark a reminder as delivered. Safe to call more than once."""
    with connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO bill_reminders(user_id,bill_rule_id,due_date) VALUES(?,?,?)",
            (reminder["user_id"], reminder["bill_rule_id"], reminder["due_date"].isoformat()),
        )
