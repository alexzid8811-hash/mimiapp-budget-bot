"""Confirmed bridge days for a delayed payroll payout."""
import json
from datetime import date


def load(con, uid):
    return {
        date.fromisoformat(row["payday"]): {
            "daily": row["daily"],
            "extensions": json.loads(row["extensions"]),
            "received_on": date.fromisoformat(row["received_on"]) if row["received_on"] else None,
        }
        for row in con.execute("SELECT * FROM payday_delays WHERE user_id=?", (uid,))
    }


def bridge_days(delay):
    return sorted(date.fromisoformat(day) for days in delay["extensions"].values() for day in days)


def expired_bridge_days(delay, received):
    return sum(day >= received for day in bridge_days(delay))
