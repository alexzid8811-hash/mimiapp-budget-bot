from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock, payday_delays
from app.backup import export_user_data, restore_user_data
from app.cashflow_app import app
from app.db import connect


@pytest.fixture
def budget(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    day = date(2026, 11, 19)
    monkeypatch.setattr(clock, "today", lambda: day)

    def advance(value):
        nonlocal day
        day = date.fromisoformat(value)

    with TestClient(app) as client:
        assert client.put("/api/payroll-settings", json={"payroll_enabled": True, "salary_gross": 150000}).is_success
        assert client.put("/api/cashflow-settings", json={
            "cashflow_enabled": True, "start_date": "2026-11-19", "start_capital": 20000,
        }).is_success
        assert client.put("/api/daily-cap", json={"amount": 1800, "excess_to": "buffer"}).is_success
        assert client.post("/api/piggy-bank/deposit", json={"amount": 10000}).is_success
        yield client, advance


def payday(client):
    data = client.get("/api/buffer").json()
    return next(row for row in data["periods"] + data["past_periods"] if row["payday"] == "2026-11-20")


def delay(client, days, request_id):
    return client.post("/api/cashflow/payday-delays/2026-11-21", json={"days": days, "request_id": request_id})


def confirm(client, actual=55000):
    return client.put("/api/cashflow/income-overrides/2026-11-21", json={"amount": actual, "confirmed": True})


def test_zero_limit_does_not_defer_new_budget_on_receipt_day():
    received = date(2026, 11, 23)
    delay = {"daily": 0, "extensions": {"extension-1": [received.isoformat()]}}
    assert payday_delays.first_regular_day(delay, received) == received


def test_delay_extends_by_new_days_and_late_advance_reallocates_remaining_days(budget):
    client, advance = budget
    bill = client.post("/api/bill-rules", json={"title": "Интернет", "amount": 5000, "day_of_month": 22}).json()
    advance("2026-11-20")
    assert payday(client)["delay_daily"] == 1800
    assert delay(client, 2, "extension-1").is_success
    assert delay(client, 2, "extension-1").is_success
    assert delay(client, 2, "extension-2").is_success
    assert client.get("/api/piggy-bank").json()["balance"] == 2800
    assert payday(client)["delay_until"] == "2026-11-24"
    assert payday(client)["to_card"] == 0

    advance("2026-11-22")
    assert client.get("/api/cashflow").json()["today_target"] == 1800
    assert client.post(f"/api/bills/{bill['id']}/pay", json={"due_date": "2026-11-22"}).is_success

    advance("2026-11-23")
    assert confirm(client).is_success
    assert confirm(client).is_success
    flow = client.get("/api/cashflow").json()
    assert flow["period"]["days_left"] == 15
    assert flow["today_target"] == 1800
    assert payday(client)["to_card"] == 14 * 1800
    assert payday(client)["received_on"] == "2026-11-23"
    assert payday(client)["funding_on"] == "2026-11-24"
    assert payday(client)["days_funded_after_delay"] == 14
    assert payday(client)["delay_repaid"] == 5400
    assert payday(client)["mandatory"] == 5000
    assert payday(client)["to_piggy"] > 0
    movements = client.get("/api/piggy-bank").json()["movements"]
    assert [row["amount"] for row in movements if row["purpose"] == "payday_delay_return"] == [1800]
    assert [row["amount"] for row in movements if row["purpose"] == "payday_delay_repayment"] == [5400]
    saved = export_user_data(1)
    restore_user_data(1, saved)
    assert client.get("/api/cashflow").json()["today_target"] == 1800


def test_actual_pay_caps_repayment_and_correction_updates_one_movement(budget):
    client, advance = budget
    advance("2026-11-20")
    assert delay(client, 3, "extension-1").is_success
    advance("2026-11-23")
    assert confirm(client, 3000).is_success
    assert payday(client)["delay_repaid"] == 3000
    assert payday(client)["delay_repayment_remaining"] == 2400
    assert payday(client)["funding_on"] == "2026-11-24"
    assert confirm(client, 55000).is_success
    assert confirm(client, 55000).is_success
    assert payday(client)["delay_repaid"] == 5400
    assert payday(client)["delay_repayment_remaining"] == 0
    movements = client.get("/api/piggy-bank").json()["movements"]
    assert [row["amount"] for row in movements if row["purpose"] == "payday_delay_repayment"] == [5400]


def test_earlier_backup_keeps_the_original_settlement_date(budget):
    client, advance = budget
    advance("2026-11-20")
    assert delay(client, 2, "extension-1").is_success
    advance("2026-11-23")
    assert confirm(client).is_success
    backup = export_user_data(1)
    backup["backup_version"] = 16
    backup["data"]["payday_delays"][0].pop("funding_on")
    backup["data"]["payday_delays"][0].pop("repayment_movement_id")
    backup["data"]["piggy_bank_movements"] = [
        row for row in backup["data"]["piggy_bank_movements"] if row["purpose"] != "payday_delay_repayment"
    ]
    restore_user_data(1, backup)
    assert payday(client)["funding_on"] == "2026-11-23"
    assert payday(client)["days_funded_after_delay"] == 15
    assert payday(client)["delay_repaid"] == 0


def test_marked_delay_remains_unreceived_after_the_next_payday(budget):
    client, advance = budget
    advance("2026-11-20")
    assert delay(client, 1, "extension-1").is_success
    advance("2026-12-08")
    assert payday(client)["income_pending"]
    assert payday(client)["received"] == 0
    assert payday(client)["to_card"] == 0
    assert delay(client, 1, "extension-2").is_success
    assert client.get("/api/cashflow").json()["today_target"] == 1800
    assert confirm(client).is_success
    with connect() as con:
        assert con.execute("SELECT received_on FROM payday_delays").fetchone()[0] == "2026-12-08"
    assert payday(client)["to_card"] == 0


def test_insufficient_piggy_funds_do_not_partially_extend_delay(budget):
    client, advance = budget
    advance("2026-11-20")
    payload = delay(client, 6, "extension-1")
    assert payload.status_code == 422
    assert client.get("/api/piggy-bank").json()["balance"] == 10000
    assert payday(client)["delay_until"] is None
    assert delay(client, 0, "extension-2").is_success
    advance("2026-12-08")
    assert payday(client)["delay_pending"]
    assert payday(client)["received"] == 0
    assert client.get("/api/piggy-bank").json()["balance"] == 10000


def test_a_bill_due_during_delay_is_reported_separately_from_card_funding(budget):
    client, advance = budget
    assert client.post("/api/bill-rules", json={"title": "Интернет", "amount": 25000, "day_of_month": 22}).is_success
    advance("2026-11-20")
    assert delay(client, 2, "extension-1").is_success
    assert payday(client)["delay_shortfall"] > 0
    assert payday(client)["mandatory"] == 25000
    assert payday(client)["delay_due"] == 25000
    assert client.get("/api/piggy-bank").json()["balance"] == 6400


def test_delay_only_counts_bills_due_by_covered_day_and_preserves_today(budget):
    client, advance = budget
    bill = client.post("/api/bill-rules", json={"title": "Счёт", "amount": 4000, "day_of_month": 23}).json()
    advance("2026-11-20")
    before = client.get("/api/cashflow").json()
    assert delay(client, 2, "extension-1").is_success
    after = client.get("/api/cashflow").json()
    for key in ("card_balance", "available_today", "today_target", "buffer_balance"):
        assert after[key] == before[key]
    row = payday(client)
    assert row["delay_check_until"] == "2026-11-22"
    assert row["delay_due"] == row["delay_shortfall"] == 0
    assert row["mandatory"] == 4000
    assert delay(client, 1, "extension-2").is_success
    assert payday(client)["delay_due"] == 4000
    advance("2026-11-24")
    assert payday(client)["delay_check_until"] == "2026-11-24"
    assert payday(client)["delay_due"] == 4000
    assert client.post(f"/api/bills/{bill['id']}/pay", json={"due_date": "2026-11-23"}).is_success
    assert payday(client)["delay_due"] == 0
    assert payday(client)["mandatory"] == 4000


def edit_delay(client, days, revision=None):
    return client.put("/api/cashflow/payday-delays/2026-11-21", json={
        "future_days": days, "revision": revision or payday(client)["delay_revision"],
    })


def test_shorten_and_cancel_delay_restore_savings_without_confirming_payday(budget):
    client, advance = budget
    advance("2026-11-20")
    before = client.get("/api/cashflow").json()
    assert delay(client, 2, "extension-one").is_success
    row = payday(client)
    assert row["delay_future_days"] == 2 and row["delay_can_cancel"]
    assert edit_delay(client, 1, row["delay_revision"]).is_success
    assert edit_delay(client, 1, row["delay_revision"]).status_code == 409
    assert delay(client, 2, "extension-one").is_success
    assert payday(client)["delay_until"] == "2026-11-21"
    assert client.get("/api/piggy-bank").json()["balance"] == 8200
    assert edit_delay(client, 8).status_code == 422
    assert payday(client)["delay_future_days"] == 1
    assert edit_delay(client, 3).is_success
    assert payday(client)["delay_until"] == "2026-11-23"
    assert client.get("/api/piggy-bank").json()["balance"] == 4600
    assert edit_delay(client, None).is_success
    row = payday(client)
    assert row["income_pending"] and not row["delay_pending"] and not row["income_confirmed"]
    assert client.get("/api/piggy-bank").json()["balance"] == 10000
    after = client.get("/api/cashflow").json()
    for key in ("card_balance", "available_today", "buffer_balance", "pending_card_allocation"):
        assert after[key] == before[key]
    restore_user_data(1, export_user_data(1))
    assert client.get("/api/piggy-bank").json()["balance"] == 10000
    assert payday(client)["income_pending"]


def test_edit_protects_current_and_elapsed_days_and_repayment_uses_them(budget):
    client, advance = budget
    advance("2026-11-20")
    assert delay(client, 3, "extension-one").is_success
    old_revision = payday(client)["delay_revision"]
    advance("2026-11-22")
    assert edit_delay(client, 0, old_revision).status_code == 409
    assert payday(client)["delay_locked_days"] == 2
    assert not payday(client)["delay_can_cancel"]
    assert edit_delay(client, None).status_code == 422
    assert edit_delay(client, 0).is_success
    assert payday(client)["delay_pending"]
    assert client.get("/api/cashflow").json()["today_target"] == 1800
    assert client.get("/api/piggy-bank").json()["balance"] == 6400
    restore_user_data(1, export_user_data(1))
    assert confirm(client).is_success
    assert payday(client)["delay_repaid"] == 3600
    assert payday(client)["funding_on"] == "2026-11-23"
    restore_user_data(1, export_user_data(1))
    assert payday(client)["delay_repaid"] == 3600
