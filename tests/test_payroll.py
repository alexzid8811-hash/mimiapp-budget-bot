from datetime import date

from app.payroll import PayrollConfig, payroll_events_between, payroll_for_accrual_month


def test_february_2026_advance_uses_workdays_1_to_15():
    cfg = PayrollConfig(salary_gross=100000, bonus_gross=20000, tax_rate=13)
    calc = payroll_for_accrual_month(2026, 2, cfg)

    assert calc["workdays_total"] == 19
    assert calc["workdays_first_half"] == 10
    assert calc["salary_net"] == 87000
    assert calc["bonus_net"] == 17400
    assert calc["advance"] == 45789.47
    assert calc["final_salary"] == 58610.53


def test_paydays_move_to_previous_workday():
    cfg = PayrollConfig(salary_gross=100000, bonus_gross=0, tax_rate=13)
    calc = payroll_for_accrual_month(2026, 2, cfg)

    assert calc["advance_nominal_date"] == "2026-02-22"
    assert calc["advance_date"] == "2026-02-20"
    assert calc["salary_nominal_date"] == "2026-03-07"
    assert calc["salary_date"] == "2026-03-06"


def test_period_gets_final_salary_for_previous_accrual_month():
    cfg = PayrollConfig(salary_gross=100000, bonus_gross=20000, tax_rate=13)
    events = payroll_events_between(date(2026, 3, 6), date(2026, 3, 19), cfg)

    salary = next(e for e in events if e["kind"] == "salary")
    assert salary["accrual_month"] == 2
    assert salary["date"] == date(2026, 3, 6)
    assert salary["amount"] == 58610.53
