from datetime import date

from app.db import connect, ensure_user, init_db
from app.reminders import pending_bill_reminders, record_bill_reminder


def test_unpaid_bill_is_reminded_once_and_paid_bill_is_not_reminded(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "reminders.sqlite3"))
    init_db()
    ensure_user(1)
    today = date(2026, 9, 17)
    with connect() as con:
        con.execute(
            "INSERT INTO bill_rules(user_id,title,amount,day_of_month) VALUES(1,'Интернет',650,20)"
        )
        con.execute(
            "INSERT INTO bill_rules(user_id,title,amount,day_of_month) VALUES(1,'Уже оплачен',100,18)"
        )
        paid_id = con.execute("SELECT id FROM bill_rules WHERE title='Уже оплачен'").fetchone()[0]
        con.execute(
            "INSERT INTO transactions(user_id,type,amount,tx_date,note,bill_rule_id,bill_due_date) "
            "VALUES(1,'expense',100,'2026-09-17','Уже оплачен',?,'2026-09-18')",
            (paid_id,),
        )

    reminders = pending_bill_reminders(today, 3)
    assert [(r["title"], r["days_left"]) for r in reminders] == [("Интернет", 3)]

    record_bill_reminder(reminders[0])
    assert pending_bill_reminders(today, 3) == []


def test_missed_three_day_window_still_sends_a_reminder(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "reminders.sqlite3"))
    init_db()
    ensure_user(1)
    with connect() as con:
        con.execute("INSERT INTO bill_rules(user_id,title,amount,day_of_month) VALUES(1,'Связь',500,19)")
    reminder = pending_bill_reminders(date(2026, 9, 17), 3)[0]
    assert reminder["days_left"] == 2
