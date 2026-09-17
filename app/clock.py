"""One calendar day for the budget, independent of the host's timezone."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo


def budget_timezone() -> str:
    return os.getenv("BUDGET_TIMEZONE", "Europe/Moscow")


def today():
    return now().date()


def now():
    return datetime.now(ZoneInfo(budget_timezone()))
