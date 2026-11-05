"""An obligatory payment not paid in its period is carried over as a debt.

The money for it stays reserved in the buffer until it is marked paid, the
debt is listed in «Платежи» of every next period, and a yearly payment is
saved up for its next year from the first payday after its date, whether
this year's one is paid or not.
"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import bot
from app import clock, engine
from app.analytics import summary
from app.cashflow_app import app
from app.db import connect, ensure_user, init_db
from app.morning_reports import _pending_report

NOW = {"day": date(2026, 10, 1)}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    NOW["day"] = date(2026, 10, 1)
    monkeypatch.setattr(clock, "today", lambda: NOW["day"])
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    init_db()
    ensure_user(1)
    with connect() as con:
        con.execute("UPDATE income_rules SET amount=60000 WHERE user_id=1")
        con.execute(
            "UPDATE settings SET cashflow_enabled=1,cashflow_start_date='2026-10-01',"
            "cashflow_start_capital=20000,forecast_months=2,payday_confirm_from='' WHERE user_id=1"
        )
    with TestClient(app) as value:
        yield value


def add_bill(client, title, amount, day, month=None):
    response = client.post("/api/bill-rules", json={
        "title": title, "amount": amount, "day_of_month": day, "month_of_year": month,
        "effective_date": "2026-10-01",
    })
    assert response.status_code == 200
    return response.json()["id"]


def plan_on(client, day):
    NOW["day"] = day
    return {(p["title"], p["due_date"]): p for p in client.get("/api/plan").json()}


def test_unpaid_bills_are_carried_to_the_next_periods_until_paid(client):
    insurance = add_bill(client, "Страховка", 11000, 6, 12)
    internet = add_bill(client, "Интернет", 1400, 8)

    # 6 December falls in the card period 21.11–07.12 (advance on Friday 20.11).
    plan = plan_on(client, date(2026, 12, 6))
    assert not plan[("Страховка", "2026-12-06")]["paid"]
    assert "debt" not in plan[("Страховка", "2026-12-06")]

    # Not paid: from the next period on it is a debt, together with the
    # November internet, while the December internet is a bill of the period.
    plan = plan_on(client, date(2026, 12, 8))
    assert plan[("Страховка", "2026-12-06")]["debt"] is True
    assert plan[("Интернет", "2026-10-08")]["debt"] is True
    assert plan[("Интернет", "2026-11-08")]["debt"] is True
    assert "debt" not in plan[("Интернет", "2026-12-08")]
    # Debts first, by date.
    assert list(plan) == [("Интернет", "2026-10-08"), ("Интернет", "2026-11-08"),
                          ("Страховка", "2026-12-06"), ("Интернет", "2026-12-08")]

    # Still a debt one period later.
    plan = plan_on(client, date(2026, 12, 24))
    assert plan[("Страховка", "2026-12-06")]["debt"] is True
    before = engine.compute(1)
    assert before["reserved_mandatory"] == 11000 + 1400 * 3  # and internet of October to December

    response = client.post(f"/api/bills/{insurance}/pay", json={"due_date": "2026-12-06"})
    assert response.status_code == 200
    assert response.json()["tx_date"] == "2026-12-24"
    after = engine.compute(1)
    # The money was waiting in the buffer: paying it changes neither the
    # buffer nor the money for today.
    assert after["reserved_mandatory"] == before["reserved_mandatory"] - 11000
    assert after["buffer_balance"] == before["buffer_balance"]
    assert after["available_today"] == before["available_today"]

    # Paid in this period: still listed, as paid, until the period ends.
    plan = plan_on(client, date(2026, 12, 24))
    assert plan[("Страховка", "2026-12-06")]["paid"] is True
    assert plan[("Страховка", "2026-12-06")]["debt"] is True
    assert plan[("Страховка", "2026-12-06")]["paid_on"] == "2026-12-24"
    plan = plan_on(client, date(2027, 1, 12))
    assert ("Страховка", "2026-12-06") not in plan
    # The unpaid internet bills are still owed.
    debts = {key for key, p in plan.items() if p.get("debt")}
    assert {("Интернет", "2026-10-08"), ("Интернет", "2026-11-08"), ("Интернет", "2026-12-08")} <= debts
    assert client.post(f"/api/bills/{internet}/pay", json={"due_date": "2026-10-08"}).status_code == 200
    assert ("Интернет", "2026-10-08") in plan_on(client, date(2027, 1, 12))
    assert not any(p.get("debt") and not p["paid"] and p["due_date"] == "2026-10-08"
                   for p in plan_on(client, date(2027, 1, 12)).values())


def test_next_year_is_saved_up_from_the_payday_after_the_date_even_if_unpaid(client):
    add_bill(client, "Страховка", 11000, 6, 12)
    NOW["day"] = date(2026, 12, 5)
    assert engine.compute(1)["yearly_saved"] == 11000
    NOW["day"] = date(2026, 12, 8)
    flow = engine.compute(1)
    # Unpaid: the 11 000 ₽ stay reserved in the buffer ...
    assert flow["reserved_mandatory"] == 11000
    # ... and the first part for 06.12.2027 is kept on the payday after the
    # date (07.12, the current buffer period), like when it is paid.
    current = flow["periods"][0]
    assert current["start"] == "2026-12-07"
    assert 11000 / 25 < current["yearly_put_aside"] == flow["yearly_saved"] < 11000 / 20


def test_bills_before_the_budget_start_are_not_debts(client):
    add_bill(client, "Интернет", 1400, 8)
    with connect() as con:
        con.execute("UPDATE settings SET cashflow_start_date='2026-11-10' WHERE user_id=1")
    plan = plan_on(client, date(2026, 11, 24))
    assert ("Интернет", "2026-11-08") not in plan
    assert engine.overdue_bills(1, date(2026, 11, 24)) == []


def test_morning_report_and_analytics_keep_the_debt(client):
    add_bill(client, "Страховка", 11000, 6, 12)
    NOW["day"] = date(2026, 12, 8)
    report = _pending_report(1, datetime(2026, 12, 8, 23, 0))
    assert report["overdue_bills"] == [{"due_date": "2026-12-06", "title": "Страховка", "amount": 11000.0}]
    text = bot.morning_report_text(report)
    assert "⚠️ Не оплачены в срок — деньги на них ждут в буфере." in text
    assert "• Страховка — 11 000,00 ₽, срок был 06.12.2026" in text
    # The analytics of December count it as still to be paid.
    row = next(b for b in summary(1, "month", date(2026, 12, 1), date(2026, 12, 8))["bills"]
               if b["title"] == "Страховка")
    assert (row["due_amount"], row["next_due"]) == (11000, "2026-12-06")


def test_many_debts_fit_the_morning_report():
    bills = [{"due_date": "2026-11-08", "title": f"Платёж {i}", "amount": 100.0} for i in range(13)]
    lines = bot.overdue_bills_lines(bills)
    assert len(lines) == 2 + bot.OVERDUE_SHOWN + 1
    assert lines[-1] == "… и ещё 3 на 300,00 ₽"
    assert bot.overdue_bills_lines([]) == []
