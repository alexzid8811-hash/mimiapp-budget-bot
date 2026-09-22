import sqlite3

from app.db import connect, ensure_user, init_db
from scripts.recover_bot_expenses import apply_recovery, build_recovery_plan


def _prepare_database(monkeypatch, path, category_id):
    monkeypatch.setenv("DATABASE_PATH", str(path))
    init_db()
    ensure_user(77, "Тест")
    with connect() as con:
        con.execute(
            "UPDATE categories SET title=?,emoji=? WHERE id=? AND user_id=?",
            ("обед", "🍲", category_id, 77),
        )
        con.execute(
            "INSERT INTO reserve_movements(user_id,period_start,amount,reason,source) "
            "VALUES(?,?,?,?, 'auto')",
            (77, "2026-09-21", 300, "Старый расчёт"),
        )


def test_recovery_imports_only_missing_expense_and_maps_category_by_title(tmp_path, monkeypatch):
    source_path = tmp_path / "bot-private.sqlite3"
    target_path = tmp_path / "shared.sqlite3"
    _prepare_database(monkeypatch, source_path, category_id=1)
    _prepare_database(monkeypatch, target_path, category_id=2)

    source = sqlite3.connect(source_path)
    source.execute(
        "INSERT INTO transactions"
        "(user_id,type,amount,tx_date,category_id,note,income_destination,created_at) "
        "VALUES(?, 'expense', ?, ?, ?, ?, 'daily', ?)",
        (77, 120, "2026-09-22", 1, "кофе", "2026-09-22 04:52:09"),
    )
    source.execute(
        "INSERT INTO transactions"
        "(user_id,type,amount,tx_date,category_id,note,income_destination,created_at) "
        "VALUES(?, 'expense', ?, ?, ?, ?, 'daily', ?)",
        (77, 406.88, "2026-09-22", 1, "дикси", "2026-09-22 10:00:00"),
    )
    source.commit()
    source.row_factory = sqlite3.Row

    target = sqlite3.connect(target_path)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys = ON")
    target.execute(
        "INSERT INTO transactions"
        "(user_id,type,amount,tx_date,category_id,note,income_destination,created_at) "
        "VALUES(?, 'expense', ?, ?, ?, ?, 'daily', ?)",
        (77, 120, "2026-09-22", 2, "кофе", "2026-09-22 04:52:09"),
    )
    target.commit()

    recovered, stats = build_recovery_plan(source, target, "2026-09-22")

    assert stats["already_present"] == 1
    assert len(recovered) == 1
    assert recovered[0].amount_cents == 40688
    assert recovered[0].target_category_id == 2

    apply_recovery(target, recovered)

    rows = target.execute(
        "SELECT amount,category_id,note FROM transactions WHERE user_id=? ORDER BY id",
        (77,),
    ).fetchall()
    auto_reserves = target.execute(
        "SELECT COUNT(*) FROM reserve_movements WHERE user_id=? AND source='auto'",
        (77,),
    ).fetchone()[0]
    source.close()
    target.close()

    assert [(row["amount"], row["category_id"], row["note"]) for row in rows] == [
        (120.0, 2, "кофе"),
        (406.88, 2, "дикси"),
    ]
    assert auto_reserves == 0
