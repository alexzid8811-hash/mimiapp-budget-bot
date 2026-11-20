"""Confirmed bridge days for a delayed payroll payout."""
import json
from datetime import date, timedelta

from .money import cents


def load(con, uid):
    return {
        date.fromisoformat(row["payday"]): {
            "daily": row["daily"],
            "extensions": json.loads(row["extensions"]),
            "received_on": date.fromisoformat(row["received_on"]) if row["received_on"] else None,
            "funding_on": date.fromisoformat(row["funding_on"]) if row["funding_on"] else None,
            "repayment_movement_id": row["repayment_movement_id"],
            "repaid_cents": cents(row["repaid"]),
        }
        for row in con.execute(
            "SELECT d.*,COALESCE(m.amount,0) AS repaid FROM payday_delays d LEFT JOIN piggy_bank_movements m "
            "ON m.user_id=d.user_id AND m.id=d.repayment_movement_id AND m.purpose='payday_delay_repayment' "
            "WHERE d.user_id=?", (uid,),
        )
    }


def bridge_days(delay):
    return sorted(date.fromisoformat(day) for days in delay["extensions"].values() for day in days)


def funding_day(delay):
    """Old receipts retain their original settlement date."""
    return delay["funding_on"] or delay["received_on"]


def covered_days(delay):
    end = funding_day(delay)
    return [day for day in bridge_days(delay) if end is None or day < end]


def first_regular_day(delay, received):
    covered = cents(delay["daily"]) > 0 and received in bridge_days(delay)
    return received + timedelta(days=int(covered))


def amount_to_repay(delay):
    return cents(delay["daily"]) * len(covered_days(delay))
