from datetime import date

from fastapi.testclient import TestClient

from app.cashflow_app import app, cashflow_period_rows
from app.db import connect, init_db


def test_period_rows_show_buffer_movements(monkeypatch):
    today = date(2026, 9, 15)
    horizon = date(2026, 10, 21)
    monkeypatch.setattr(
        "app.cashflow_app.payday_boundaries",
        lambda *_args, **_kwargs: [
            {"date": date(2026, 9, 22), "kind": "аванс"},
            {"date": date(2026, 10, 7), "kind": "зарплата"},
        ],
    )
    monkeypatch.setattr(
        "app.cashflow_app.planned_income_map",
        lambda *_args, **_kwargs: {
            date(2026, 9, 22): 1000,
            date(2026, 10, 7): 2000,
        },
    )
    monkeypatch.setattr(
        "app.cashflow_app.planned_mandatory_map",
        lambda *_args, **_kwargs: {date(2026, 9, 25): 500},
    )

    rows = cashflow_period_rows(
        1,
        today=today,
        horizon_end=horizon,
        opening_balance_before_today_spend=1000,
        daily_target=100,
    )

    assert rows[0]["kind"] == "сейчас"
    assert rows[0]["days"] == 7
    assert rows[0]["put_aside"] == 300
    assert rows[0]["buffer"] == 300
    assert rows[1]["kind"] == "аванс"
    assert rows[1]["mandatory"] == 500
    assert rows[1]["take"] == 300
    assert rows[1]["buffer"] == 0


def test_piggy_bank_changes_spending_cash_and_rejects_overdraft(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    init_db()

    with TestClient(app) as client:
        settings = client.put(
            "/api/cashflow-settings",
            json={
                "cashflow_enabled": True,
                "start_date": date.today().isoformat(),
                "start_capital": 1000,
            },
        )
        assert settings.status_code == 200

        deposit = client.post(
            "/api/piggy-bank/deposit",
            json={"amount": 300, "movement_date": date.today().isoformat(), "note": "Резерв"},
        )
        assert deposit.status_code == 200
        assert deposit.json()["balance"] == 300

        flow = client.get("/api/cashflow")
        assert flow.status_code == 200
        assert flow.json()["current_cash"] == 700
        assert flow.json()["piggy_bank_balance"] == 300

        too_much = client.post(
            "/api/piggy-bank/withdraw",
            json={"amount": 301, "movement_date": date.today().isoformat(), "note": ""},
        )
        assert too_much.status_code == 422

        withdrawn = client.post(
            "/api/piggy-bank/withdraw",
            json={"amount": 100, "movement_date": date.today().isoformat(), "note": "Вернул"},
        )
        assert withdrawn.status_code == 200
        assert withdrawn.json()["balance"] == 200

        with connect() as con:
            directions = [
                row[0]
                for row in con.execute(
                    "SELECT direction FROM piggy_bank_movements WHERE user_id=1 ORDER BY id"
                )
            ]
        assert directions == ["deposit", "withdraw"]
