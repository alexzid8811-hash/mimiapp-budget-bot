from datetime import datetime

from app.db import connect, ensure_user, init_db
from app.morning_reports import pending_morning_reports, record_morning_report


def test_report_lists_every_yesterday_expense(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "reports.sqlite3"))
    init_db()
    ensure_user(1)
    with connect() as con:
        con.execute("UPDATE settings SET morning_report_time='06:00' WHERE user_id=1")
        con.executemany(
            "INSERT INTO transactions(user_id,type,amount,tx_date,note) VALUES(1,'expense',?,?,?)",
            [(183, "2026-09-21", "Проезд"), (240, "2026-09-21", "кофе"), (1009.18, "2026-09-21", "Пятёрочка")],
        )
    monkeypatch.setattr("app.morning_reports.cashflow_snapshot", lambda _uid: {
        "current_cash": 2066.97, "available_today": 500, "today_target": 600,
        "remaining_period": 1000, "piggy_bank_balance": 20, "buffer_balance": 30,
    })
    monkeypatch.setattr("app.morning_reports.planning.user_period", lambda *_: type("Period", (), {"end": datetime(2026, 9, 27).date()})())
    monkeypatch.setattr("app.morning_reports.planning.bill_events", lambda *_: [])
    report = pending_morning_reports(datetime(2026, 9, 22, 7, 0))[0]
    assert [item["title"] for item in report["expenses"]] == ["Проезд", "кофе", "Пятёрочка"]
    assert report["spent_total"] == 1432.18
    record_morning_report(report)
