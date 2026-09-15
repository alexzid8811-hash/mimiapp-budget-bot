from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

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


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _vacation_bounds(vacation: Any) -> tuple[date, date]:
    if isinstance(vacation, dict):
        return _as_date(vacation["start_date"]), _as_date(vacation["end_date"])
    return _as_date(vacation.start_date), _as_date(vacation.end_date)


def _vacation_value(vacation: Any, key: str, default: Any = None) -> Any:
    if isinstance(vacation, dict):
        return vacation.get(key, default)
    return getattr(vacation, key, default)


def vacation_workdays_in_month(year: int, month: int, vacations: Iterable[Any] | None = None) -> set[date]:
    if not vacations:
        return set()
    month_workdays = set(working_days_in_month(year, month))
    result: set[date] = set()
    for vacation in vacations:
        start, end = _vacation_bounds(vacation)
        if end < start:
            continue
        for day in month_workdays:
            if start <= day <= end:
                result.add(day)
    return result


def payroll_for_accrual_month(
    year: int,
    month: int,
    config: PayrollConfig,
    vacations: Iterable[Any] | None = None,
) -> dict:
    workdays = working_days_in_month(year, month)
    first_half = [d for d in workdays if d.day <= 15]
    vacation_days = vacation_workdays_in_month(year, month, vacations)
    worked_days = [d for d in workdays if d not in vacation_days]
    worked_first_half = [d for d in first_half if d not in vacation_days]

    salary_net = net_after_tax(config.salary_gross, config.tax_rate)
    bonus_net = net_after_tax(config.bonus_gross, config.tax_rate)

    if workdays:
        salary_net_for_worked_days = round(salary_net * len(worked_days) / len(workdays), 2)
        advance = round(salary_net * len(worked_first_half) / len(workdays), 2)
    else:
        salary_net_for_worked_days = 0.0
        advance = 0.0
    final_salary = round(max(0.0, salary_net_for_worked_days - advance) + bonus_net, 2)

    accrual_month = date(year, month, 1)
    next_month = add_months(accrual_month, 1).replace(day=1)
    advance_nominal = month_date(year, month, config.advance_day)
    final_nominal = month_date(next_month.year, next_month.month, config.salary_day)

    return {
        "accrual_year": year,
        "accrual_month": month,
        "workdays_total": len(workdays),
        "workdays_first_half": len(first_half),
        "vacation_workdays": len(vacation_days),
        "worked_days_total": len(worked_days),
        "worked_days_first_half": len(worked_first_half),
        "salary_gross": round(float(config.salary_gross), 2),
        "bonus_gross": round(float(config.bonus_gross), 2),
        "tax_rate": round(float(config.tax_rate), 2),
        "salary_net": salary_net,
        "salary_net_for_worked_days": salary_net_for_worked_days,
        "bonus_net": bonus_net,
        "advance": advance,
        "final_salary": final_salary,
        "advance_nominal_date": advance_nominal.isoformat(),
        "advance_date": payday_on_or_before(advance_nominal).isoformat(),
        "salary_nominal_date": final_nominal.isoformat(),
        "salary_date": payday_on_or_before(final_nominal).isoformat(),
    }


def payroll_events_between(
    start: date,
    end: date,
    config: PayrollConfig,
    vacations: Iterable[Any] | None = None,
) -> list[dict]:
    """Return calculated payroll and vacation-pay events inside the range."""
    vacations = list(vacations or [])
    cursor = add_months(start.replace(day=1), -2).replace(day=1)
    last = add_months(end.replace(day=1), 1).replace(day=1)
    events: list[dict] = []

    while cursor <= last:
        calc = payroll_for_accrual_month(cursor.year, cursor.month, config, vacations)
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

    for vacation in vacations:
        amount = max(0.0, float(_vacation_value(vacation, "amount", 0) or 0))
        payment_value = _vacation_value(vacation, "payment_date")
        if amount <= 0 or not payment_value:
            continue
        payment_date = _as_date(payment_value)
        if start <= payment_date <= end:
            events.append({
                "kind": "vacation_pay",
                "title": "Отпускные",
                "amount": round(amount, 2),
                "date": payment_date,
                "nominal_date": payment_date,
                "vacation_id": _vacation_value(vacation, "id"),
                "vacation_start": _vacation_bounds(vacation)[0],
                "vacation_end": _vacation_bounds(vacation)[1],
            })

    order = {"vacation_pay": 0, "advance": 1, "salary": 2}
    events.sort(key=lambda e: (e["date"], order.get(e["kind"], 9)))
    return events
