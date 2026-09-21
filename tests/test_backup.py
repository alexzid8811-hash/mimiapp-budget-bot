import sqlite3

from app.backup import export_user_data, restore_user_data
from app.db import connect, ensure_user, init_db


def test_category_order_migration_preserves_existing_sequence(tmp_path, monkeypatch):
    database = tmp_path / "legacy.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(database))
    with sqlite3.connect(database) as con:
        con.execute(
            "CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
            "title TEXT NOT NULL, emoji TEXT NOT NULL DEFAULT '💳', "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id,title))"
        )
        con.execute("INSERT INTO categories(user_id,title,emoji) VALUES(1,'Первая','1️⃣')")
        con.execute("INSERT INTO categories(user_id,title,emoji) VALUES(1,'Вторая','2️⃣')")

    init_db()
    with connect() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(categories)")}
        rows = con.execute(
            "SELECT title,sort_order FROM categories WHERE user_id=1 ORDER BY sort_order,id"
        ).fetchall()
    assert "sort_order" in columns
    assert [tuple(row) for row in rows] == [("Первая", 1), ("Вторая", 2)]


def test_backup_round_trip_remaps_relations(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    init_db()
    ensure_user(1, "Alex")

    with connect() as con:
        category_id = con.execute(
            "INSERT INTO categories(user_id,title,emoji) VALUES(1,'Резервная категория','🧰')"
        ).lastrowid
        bill_id = con.execute(
            "INSERT INTO bill_rules(user_id,title,amount,day_of_month,category_id) VALUES(1,'Аренда',30000,10,?)",
            (category_id,),
        ).lastrowid
        con.execute(
            "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note,bill_rule_id,bill_due_date) "
            "VALUES(1,'expense',30000,'2026-09-10',?,'Оплата',?,'2026-09-10')",
            (category_id, bill_id),
        )
        con.execute("UPDATE settings SET salary_gross=123456,payroll_enabled=1 WHERE user_id=1")
        con.execute(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note) "
            "VALUES(1,'deposit',5000,'2026-09-12','На чёрный день')"
        )

    backup = export_user_data(1)
    with connect() as con:
        con.execute("DELETE FROM transactions WHERE user_id=1")
        con.execute("DELETE FROM bill_rules WHERE user_id=1")
        con.execute("DELETE FROM categories WHERE user_id=1")
        con.execute("UPDATE settings SET salary_gross=0,payroll_enabled=0 WHERE user_id=1")

    restored = restore_user_data(1, backup)
    assert restored["transactions"] == 1
    with connect() as con:
        tx = con.execute(
            "SELECT t.note,c.title,b.title FROM transactions t "
            "JOIN categories c ON c.id=t.category_id JOIN bill_rules b ON b.id=t.bill_rule_id "
            "WHERE t.user_id=1"
        ).fetchone()
        settings = con.execute("SELECT salary_gross,payroll_enabled FROM settings WHERE user_id=1").fetchone()
    assert tuple(tx) == ("Оплата", "Резервная категория", "Аренда")
    assert tuple(settings) == (123456.0, 1)
    with connect() as con:
        piggy = con.execute(
            "SELECT direction,amount,note FROM piggy_bank_movements WHERE user_id=1"
        ).fetchone()
    assert tuple(piggy) == ("deposit", 5000.0, "На чёрный день")


def test_invalid_backup_does_not_delete_existing_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    init_db()
    ensure_user(1, "Alex")

    try:
        restore_user_data(1, {"backup_version": 999})
    except ValueError:
        pass
    else:
        raise AssertionError("invalid backup must be rejected")

    with connect() as con:
        count = con.execute("SELECT COUNT(*) FROM categories WHERE user_id=1").fetchone()[0]
    assert count == 8


def test_corrupt_rows_roll_back_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    init_db()
    ensure_user(1, "Alex")
    backup = export_user_data(1)
    backup["data"]["income_rules"] = [{"id": 1, "title": "Ошибка", "day_of_month": 99}]

    try:
        restore_user_data(1, backup)
    except ValueError:
        pass
    else:
        raise AssertionError("corrupt rows must be rejected")

    with connect() as con:
        count = con.execute("SELECT COUNT(*) FROM categories WHERE user_id=1").fetchone()[0]
    assert count == 8
