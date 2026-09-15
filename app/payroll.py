from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from .budget import add_months, month_date
from .russian_calendar import is_working_day_ru, payday_on_or_before


@dataclass(frozen=True)
class PayrollConfig:
    salary_gross: float = 0.0
    bonus_gross: float = 0.0
    tax_rate: float = 13.0
    salary_day: int = 7
    advance_day: int = 22


def net_after_tax(amount: float, tax_rate: float) -> float:
    rate = min(100.0, max(0.0, float(tax_rate))) / 100.0
    return round(max(0.0, float(amount)) * (1.0 - rate), 2)


def working_days_in_month(year: int, month: int) -> list[date]:
    last = calendar.monthrange(year, month)[1]
    return [date(year, month, day) for day in range(1, last + 1) if is_working_day_ru(date(year, month, day))]


def payroll_for_accrual_month(year: int, month: int, config: PayrollConfig) -> dict:
    workdays = working_days_in_month(year, month)
    first_half = [d for d in workdays if d.day <= 15]

    salary_net = net_after_tax(config.salary_gross, config.tax_rate)
    bonus_net = net_after_tax(config.bonus_gross, config.tax_rate)

    if workdays:
        advance = round(salary_net * len(first_half) / len(workdays), 2)
    else:
        advance = 0.0
    final_salary = round(max(0.0, salary_net - advance) + bonus_net, 2)

    accrual_month = date(year, month, 1)
    next_month = add_months(accrual_month, 1).replace(day=1)
    advance_nominal = month_date(year, month, config.advance_day)
    final_nominal = month_date(next_month.year, next_month.month, config.salary_day)

    return {
        "accrual_year": year,
        "accrual_month": month,
        "workdays_total": len(workdays),
        "workdays_first_half": len(first_half),
        "salary_gross": round(float(config.salary_gross), 2),
        "bonus_gross": round(float(config.bonus_gross), 2),
        "tax_rate": round(float(config.tax_rate), 2),
        "salary_net": salary_net,
        "bonus_net": bonus_net,
        "advance": advance,
        "final_salary": final_salary,
        "advance_nominal_date": advance_nominal.isoformat(),
        "advance_date": payday_on_or_before(advance_nominal).isoformat(),
        "salary_nominal_date": final_nominal.isoformat(),
        "salary_date": payday_on_or_before(final_nominal).isoformat(),
    }


def payroll_events_between(start: date, end: date, config: PayrollConfig) -> list[dict]:
    """Return calculated salary/advance payments whose actual payment date is inside the range."""
    cursor = add_months(start.replace(day=1), -2).replace(day=1)
    last = add_months(end.replace(day=1), 1).replace(day=1)
    events: list[dict] = []

    while cursor <= last:
        calc = payroll_for_accrual_month(cursor.year, cursor.month, config)
        for kind, amount_key, date_key, nominal_key, title in [
            ("advance", "advance", "advance_date", "advance_nominal_date", "Аванс"),
            ("salary", "final_salary", "salary_date", "salary_nominal_date", "Зарплата + премия"),
        ]:
            payment_date = date.fromisoformat(calc[date_key])
            if start <= payment_date <= end:
                events.append({
                    "kind": kind,
                    "title": title,
                    "amount": float(calc[amount_key]),
                    "date": payment_date,
                    "nominal_date": date.fromisoformat(calc[nominal_key]),
                    "accrual_year": cursor.year,
                    "accrual_month": cursor.month,
                    "calculation": calc,
                })
        cursor = add_months(cursor, 1).replace(day=1)

    events.sort(key=lambda e: (e["date"], e["kind"]))
    return events
