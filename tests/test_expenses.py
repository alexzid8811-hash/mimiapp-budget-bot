from datetime import date

import pytest

from app import clock, planning
from app.db import connect, ensure_user, init_db
from app.expenses import (
    categories_for_user,
    create_expense,
    delete_expense,
    expense_for_user,
    update_expense,
)


@pytest.fixture
def expense_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "expenses.sqlite3"))
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 22))
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    init_db()
    ensure_user(1)
    return 1


def test_chat_expense_is_written_to_shared_transactions_and_invalidates_reserve(expense_database):
    user_id = expense_database
    category = categories_for_user(user_id)[0]
    period = planning.user_period(user_id, clock.today())
    with connect() as con:
        con.execute(
            "INSERT INTO reserve_movements(user_id,period_start,amount,reason,source) "
            "VALUES(?,?,?,?, 'auto')",
            (user_id, period.start.isoformat(), 500, "Старый расчёт"),
        )

    transaction = create_expense(
        user_id, 1_250.555, category["id"], note="Продукты", tx_date=clock.today()
    )

    assert transaction["amount"] == 1250.56
    assert transaction["category_title"] == category["title"]
    with connect() as con:
        stored = dict(con.execute("SELECT * FROM transactions WHERE id=?", (transaction["id"],)).fetchone())
        reserves = con.execute(
            "SELECT COUNT(*) FROM reserve_movements WHERE user_id=? AND source='auto'",
            (user_id,),
        ).fetchone()[0]
    assert stored["type"] == "expense"
    assert stored["amount"] == 1250.56
    assert stored["category_id"] == category["id"]
    assert stored["note"] == "Продукты"
    assert reserves == 0


def test_chat_expense_cannot_use_another_users_category(expense_database):
    ensure_user(2)
    foreign_category = categories_for_user(2)[0]

    with pytest.raises(ValueError, match="Категория не найдена"):
        create_expense(expense_database, 100, foreign_category["id"])


def test_chat_expense_can_be_undone_only_by_its_owner(expense_database):
    category = categories_for_user(expense_database)[0]
    transaction = create_expense(expense_database, 300, category["id"])

    assert delete_expense(2, transaction["id"]) is False
    assert delete_expense(expense_database, transaction["id"]) is True
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_chat_expense_can_be_edited_without_creating_a_second_transaction(expense_database):
    categories = categories_for_user(expense_database)
    transaction = create_expense(expense_database, 300, categories[0]["id"], note="Кофе")
    period = planning.user_period(expense_database, clock.today())
    with connect() as con:
        con.execute(
            "INSERT INTO reserve_movements(user_id,period_start,amount,reason,source) "
            "VALUES(?,?,?,?, 'auto')",
            (expense_database, period.start.isoformat(), 500, "Старый расчёт"),
        )

    changed = update_expense(
        expense_database,
        transaction["id"],
        amount=406.88,
        category_id=categories[1]["id"],
        note="Обед · Дикси",
    )

    assert changed["id"] == transaction["id"]
    assert changed["amount"] == 406.88
    assert changed["category_id"] == categories[1]["id"]
    assert changed["note"] == "Обед · Дикси"
    assert expense_for_user(expense_database, transaction["id"]) == changed
    with connect() as con:
        count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        reserves = con.execute(
            "SELECT COUNT(*) FROM reserve_movements WHERE user_id=? AND source='auto'",
            (expense_database,),
        ).fetchone()[0]
    assert count == 1
    assert reserves == 0
