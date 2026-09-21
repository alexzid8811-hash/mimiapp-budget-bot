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


def test_vacation_reduces_salary_by_workdays_not_by_vacation_pay_amount():
    cfg = PayrollConfig(salary_gross=100000, bonus_gross=0, tax_rate=13)
    vacations = [{
        "id": 1,
        "start_date": "2026-02-02",
        "end_date": "2026-02-06",
        "amount": 120000,
        "payment_date": "2026-01-30",
    }]
    calc = payroll_for_accrual_month(2026, 2, cfg, vacations)
    assert calc["vacation_workdays"] == 5
    assert calc["worked_days_total"] == 14
    assert calc["worked_days_first_half"] == 5
    assert calc["advance"] == 22894.74
    assert calc["salary_net_for_worked_days"] == 64105.26
    assert calc["final_salary"] == 41210.52


def test_vacation_pay_is_separate_income_and_can_exceed_salary():
    cfg = PayrollConfig(salary_gross=100000, bonus_gross=0, tax_rate=13)
    vacations = [{
        "id": 7,
        "start_date": "2026-02-02",
        "end_date": "2026-02-06",
        "amount": 120000,
        "payment_date": "2026-01-30",
    }]
    events = payroll_events_between(date(2026, 1, 30), date(2026, 2, 20), cfg, vacations)
    vacation_pay = next(e for e in events if e["kind"] == "vacation_pay")
    advance = next(e for e in events if e["kind"] == "advance" and e["accrual_month"] == 2)
    assert vacation_pay["amount"] == 120000
    assert vacation_pay["date"] == date(2026, 1, 30)
    assert advance["amount"] == 22894.74


def test_second_half_vacation_keeps_advance_and_reduces_final_salary():
    cfg = PayrollConfig(salary_gross=100000, bonus_gross=0, tax_rate=13)
    vacations = [{
        "start_date": "2026-02-16",
        "end_date": "2026-02-20",
        "amount": 90000,
        "payment_date": "2026-02-13",
    }]
    calc = payroll_for_accrual_month(2026, 2, cfg, vacations)
    assert calc["vacation_workdays"] == 5
    assert calc["advance"] == 45789.47
    assert calc["salary_net_for_worked_days"] == 64105.26
    assert calc["final_salary"] == 18315.79


def test_january_2027_advance_excludes_long_new_year_holidays(monkeypatch):
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda year: "0" * 365)
    calc = payroll_for_accrual_month(
        2027, 1, PayrollConfig(salary_gross=100000, bonus_gross=0, tax_rate=13)
    )
    assert calc["workdays_total"] == 15
    assert calc["workdays_first_half"] == 5
    assert calc["advance"] == 29000
    assert calc["advance_date"] == "2027-01-22"


def test_payroll_events_can_use_a_scheduled_config_by_accrual_month():
    old = PayrollConfig(salary_gross=100000, bonus_gross=0, tax_rate=13)
    raised = PayrollConfig(salary_gross=110000, bonus_gross=15000, tax_rate=13)

    def config_for_month(year, month):
        return raised if (year, month) >= (2026, 11) else old

    events = payroll_events_between(date(2026, 11, 1), date(2026, 12, 15), config_for_month)
    october_salary = next(event for event in events if event["kind"] == "salary" and event["accrual_month"] == 10)
    november_advance = next(event for event in events if event["kind"] == "advance" and event["accrual_month"] == 11)
    november_salary = next(event for event in events if event["kind"] == "salary" and event["accrual_month"] == 11)

    assert october_salary["amount"] == 43500
    assert november_advance["amount"] > 43500
    assert november_salary["amount"] > october_salary["amount"]
