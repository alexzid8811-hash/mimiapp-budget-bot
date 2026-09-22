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
        use_buffer_boundaries=True,
    )

    assert rows[0]["kind"] == "сейчас"
    # Buffer rows start on the actual payday, while card periods start the
    # following day.  The first partial buffer segment therefore ends before
    # the 22 September payment.
    assert rows[0]["days"] == 6
    assert rows[0]["put_aside"] == 400
    assert rows[0]["buffer"] == 400
    assert rows[1]["kind"] == "аванс"
    assert rows[1]["mandatory"] == 500
    assert rows[1]["start"] == "2026-09-21"
    assert rows[1]["budget_start"] == "2026-09-22"
    assert rows[1]["take"] == 400
    assert rows[1]["buffer"] == 0


def test_piggy_bank_is_separate_until_daily_remainder_is_explicitly_transferred(tmp_path, monkeypatch):
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
        assert flow.json()["current_cash"] == 1000
        assert flow.json()["piggy_bank_balance"] == 300

        transfer_amount = flow.json()["available_today"]
        assert transfer_amount > 0
        transfer = client.post(
            "/api/piggy-bank/deposit",
            json={
                "amount": transfer_amount,
                "movement_date": date.today().isoformat(),
                "note": "Остаток дня",
                "source": "daily_budget",
            },
        )
        assert transfer.status_code == 200
        assert client.get("/api/cashflow").json()["current_cash"] == round(1000 - transfer_amount, 2)

        too_much = client.post(
            "/api/piggy-bank/withdraw",
            json={"amount": 301 + transfer_amount, "movement_date": date.today().isoformat(), "note": ""},
        )
        assert too_much.status_code == 422

        withdrawn = client.post(
            "/api/piggy-bank/withdraw",
            json={"amount": 100, "movement_date": date.today().isoformat(), "note": "Вернул"},
        )
        assert withdrawn.status_code == 200
        assert withdrawn.json()["balance"] == round(300 + transfer_amount - 100, 2)
        assert client.get("/api/cashflow").json()["current_cash"] == round(1000 - transfer_amount, 2)

        with connect() as con:
            directions = [
                row[0]
                for row in con.execute(
                    "SELECT direction FROM piggy_bank_movements WHERE user_id=1 ORDER BY id"
                )
            ]
        assert directions == ["deposit", "deposit", "withdraw"]


def test_daily_remainder_transfer_cannot_exceed_available_today(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    init_db()

    with TestClient(app) as client:
        client.put(
            "/api/cashflow-settings",
            json={
                "cashflow_enabled": True,
                "start_date": date.today().isoformat(),
                "start_capital": 100,
            },
        )
        available = client.get("/api/cashflow").json()["available_today"]
        response = client.post(
            "/api/piggy-bank/deposit",
            json={
                "amount": available + 0.01,
                "movement_date": date.today().isoformat(),
                "source": "daily_budget",
            },
        )
        assert response.status_code == 422


def test_piggy_transfer_to_card_increases_only_card_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    init_db()

    with TestClient(app) as client:
        client.put(
            "/api/cashflow-settings",
            json={
                "cashflow_enabled": True,
                "start_date": date.today().isoformat(),
                "start_capital": 1000,
            },
        )
        assert client.post(
            "/api/piggy-bank/deposit",
            json={"amount": 300, "movement_date": date.today().isoformat(), "note": "Накопления"},
        ).status_code == 200
        before = client.get("/api/cashflow").json()

        moved = client.post(
            "/api/piggy-bank/withdraw",
            json={
                "amount": 120,
                "movement_date": date.today().isoformat(),
                "note": "Покрытие перерасхода",
                "source": "daily_budget",
            },
        )
        assert moved.status_code == 200
        after = client.get("/api/cashflow").json()

        assert moved.json()["movement"]["source"] == "daily_budget"
        assert after["piggy_bank_balance"] == 180
        assert after["current_cash"] == before["current_cash"] + 120
        assert after["buffer_balance"] == before["buffer_balance"]


def test_buffer_table_starts_with_recorded_start_capital(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    init_db()

    with TestClient(app) as client:
        client.put(
            "/api/cashflow-settings",
            json={
                "cashflow_enabled": True,
                "start_date": date.today().isoformat(),
                "start_capital": 39000,
            },
        )
        buffer = client.get("/api/buffer").json()

    first = buffer["periods"][0]
    assert first["initial_capital"] is True
    assert first["kind"] == "Стартовый капитал"
    assert first["received"] == 39000
    assert first["received_editable"] is False
