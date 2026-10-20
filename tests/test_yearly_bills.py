from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock, planning
from app.cashflow_app import app
from app.db import connect, ensure_user, init_db
from app.morning_reports import _nearest_unpaid_bills

TODAY = date(2026, 9, 13)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    init_db()
    ensure_user(1)
    with TestClient(app) as value:
        yield value


def add_yearly(client, **extra):
    response = client.post("/api/bill-rules", json={
        "title": "ОСАГО", "amount": 12000, "day_of_month": 15, "month_of_year": 3, **extra,
    })
    assert response.status_code == 200
    return response.json()


def due_dates(bill_id, start, end):
    return [e["due_date"] for e in planning.bill_events(1, start, end) if e["id"] == bill_id]


def test_yearly_bill_is_due_once_a_year(client):
    bill = add_yearly(client)
    assert bill["month_of_year"] == 3
    assert due_dates(bill["id"], TODAY, date(2028, 12, 31)) == ["2027-03-15", "2028-03-15"]
    assert planning.mandatory_map(1, TODAY, date(2027, 12, 31)) == {date(2027, 3, 15): 12000}


def test_yearly_bill_is_paid_on_its_date(client):
    bill = add_yearly(client)
    assert client.post(f"/api/bills/{bill['id']}/pay", json={"due_date": "2027-02-15"}).status_code == 422
    assert client.post(f"/api/bills/{bill['id']}/pay", json={"due_date": "2027-03-15"}).status_code == 200
    events = [e for e in planning.bill_events(1, TODAY, date(2027, 12, 31)) if e["id"] == bill["id"]]
    assert [(e["due_date"], e["paid"]) for e in events] == [("2027-03-15", True)]


def test_switching_between_monthly_and_yearly(client):
    bill = add_yearly(client)
    edited = client.put(f"/api/bill-rules/{bill['id']}", json={
        "title": "ОСАГО", "amount": 1000, "day_of_month": 15, "month_of_year": None,
    })
    assert edited.status_code == 200 and edited.json()["month_of_year"] is None
    assert due_dates(bill["id"], TODAY, date(2026, 11, 30)) == ["2026-09-15", "2026-10-15", "2026-11-15"]
    assert client.post("/api/bill-rules", json={
        "title": "X", "amount": 1, "day_of_month": 1, "month_of_year": 13,
    }).status_code == 422


def test_morning_report_sees_next_yearly_date(client):
    add_yearly(client)
    with connect() as con:
        con.execute("DELETE FROM bill_rules WHERE user_id=1 AND month_of_year IS NULL")
    assert _nearest_unpaid_bills(1, TODAY) == (
        "2027-03-15", [{"due_date": "2027-03-15", "title": "ОСАГО", "amount": 12000.0}],
    )


def test_backup_keeps_yearly_bill(client):
    from app.backup import export_user_data, restore_user_data

    bill = add_yearly(client)
    data = export_user_data(1)
    restore_user_data(1, data)
    with connect() as con:
        rows = con.execute("SELECT id,month_of_year FROM bill_rules WHERE user_id=? AND title='ОСАГО'", (1,)).fetchall()
    assert [r["month_of_year"] for r in rows] == [3]
    assert due_dates(rows[0]["id"], TODAY, date(2027, 12, 31)) == ["2027-03-15"]
