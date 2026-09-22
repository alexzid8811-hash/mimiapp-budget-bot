#!/usr/bin/env python3
"""Recover ordinary expenses from an old, bot-local SQLite database.

Before the bot was given the same ``/app/data`` volume as the Mini App, a
quick expense could be written to the bot container's private database.  This
tool reads a copy of that database and adds only missing ordinary expenses to
the shared database.  It is a dry run by default.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


@dataclass(frozen=True)
class Expense:
    source_id: int
    user_id: int
    amount_cents: int
    tx_date: str
    category_title: str | None
    note: str
    created_at: str
    target_category_id: int | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Перенести обычные траты из старой локальной базы бота в общую базу Mini App. "
            "Без --apply выполняется только проверка."
        )
    )
    parser.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Копия /app/data/budget.sqlite3 из старого контейнера bot",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path(os.getenv("DATABASE_PATH", "./data/budget.sqlite3")),
        help="Общая база Mini App (по умолчанию DATABASE_PATH)",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Переносить операции этой даты и позднее",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Записать найденные операции. Без флага база не меняется.",
    )
    return parser.parse_args(argv)


def amount_cents(value: object) -> int:
    return int(
        (Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def amount_for_sql(amount_in_cents: int) -> float:
    return amount_in_cents / 100


def money(amount_in_cents: int) -> str:
    return f"{amount_for_sql(amount_in_cents):,.2f}".replace(",", " ").replace(".", ",")


def open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"Не найден файл базы-источника: {path}")
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def open_target(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"Не найден файл общей базы: {path}")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def require_schema(con: sqlite3.Connection, database_name: str) -> None:
    tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required_tables = {"users", "categories", "transactions", "reserve_movements"}
    missing_tables = required_tables - tables
    if missing_tables:
        names = ", ".join(sorted(missing_tables))
        raise ValueError(f"В {database_name} нет обязательных таблиц: {names}")

    columns = {
        row[1]
        for row in con.execute("PRAGMA table_info(transactions)").fetchall()
    }
    required_columns = {
        "id",
        "user_id",
        "type",
        "amount",
        "tx_date",
        "category_id",
        "note",
        "bill_rule_id",
        "created_at",
    }
    missing_columns = required_columns - columns
    if missing_columns:
        names = ", ".join(sorted(missing_columns))
        raise ValueError(f"В {database_name}.transactions нет полей: {names}")


def transaction_key(
    user_id: int,
    amount_in_cents: int,
    tx_date: str,
    category_title: str | None,
    note: str,
) -> tuple[int, int, str, str | None, str]:
    return (user_id, amount_in_cents, tx_date, category_title, note)


def source_expenses(
    con: sqlite3.Connection, since: str | None
) -> list[sqlite3.Row]:
    where_since = " AND t.tx_date >= ?" if since else ""
    params: tuple[str, ...] = (since,) if since else ()
    return con.execute(
        "SELECT t.id,t.user_id,t.amount,t.tx_date,t.note,t.created_at,"
        "c.title AS category_title "
        "FROM transactions t LEFT JOIN categories c "
        "ON c.id=t.category_id AND c.user_id=t.user_id "
        "WHERE t.type='expense' AND t.bill_rule_id IS NULL"
        f"{where_since} ORDER BY t.user_id,t.tx_date,t.id",
        params,
    ).fetchall()


def existing_expense_counts(con: sqlite3.Connection) -> Counter:
    rows = con.execute(
        "SELECT t.user_id,t.amount,t.tx_date,t.note,c.title AS category_title "
        "FROM transactions t LEFT JOIN categories c "
        "ON c.id=t.category_id AND c.user_id=t.user_id "
        "WHERE t.type='expense' AND t.bill_rule_id IS NULL"
    ).fetchall()
    return Counter(
        transaction_key(
            int(row["user_id"]),
            amount_cents(row["amount"]),
            str(row["tx_date"]),
            row["category_title"],
            str(row["note"] or ""),
        )
        for row in rows
    )


def target_category_id(
    con: sqlite3.Connection, user_id: int, category_title: str | None
) -> int | None:
    if category_title is None:
        return None
    row = con.execute(
        "SELECT id FROM categories WHERE user_id=? AND title=?",
        (user_id, category_title),
    ).fetchone()
    return int(row["id"]) if row else None


def build_recovery_plan(
    source: sqlite3.Connection, target: sqlite3.Connection, since: str | None
) -> tuple[list[Expense], Counter]:
    target_users = {
        int(row["id"]) for row in target.execute("SELECT id FROM users").fetchall()
    }
    already_present = existing_expense_counts(target)
    stats: Counter = Counter()
    planned: list[Expense] = []
    category_cache: dict[tuple[int, str | None], int | None] = {}

    for row in source_expenses(source, since):
        stats["reviewed"] += 1
        user_id = int(row["user_id"])
        category_title = row["category_title"]
        if user_id not in target_users:
            stats["skipped_unknown_user"] += 1
            continue

        cache_key = (user_id, category_title)
        if cache_key not in category_cache:
            category_cache[cache_key] = target_category_id(target, user_id, category_title)
        category_id = category_cache[cache_key]
        if category_title is not None and category_id is None:
            stats["skipped_unknown_category"] += 1
            continue

        cents = amount_cents(row["amount"])
        note = str(row["note"] or "")
        key = transaction_key(
            user_id, cents, str(row["tx_date"]), category_title, note
        )
        if already_present[key]:
            already_present[key] -= 1
            stats["already_present"] += 1
            continue

        planned.append(
            Expense(
                source_id=int(row["id"]),
                user_id=user_id,
                amount_cents=cents,
                tx_date=str(row["tx_date"]),
                category_title=category_title,
                note=note,
                created_at=str(row["created_at"]),
                target_category_id=category_id,
            )
        )

    return planned, stats


def apply_recovery(target: sqlite3.Connection, expenses: list[Expense]) -> None:
    if not expenses:
        return
    target.execute("BEGIN IMMEDIATE")
    try:
        target.executemany(
            "INSERT INTO transactions"
            "(user_id,type,amount,tx_date,category_id,note,income_destination,created_at) "
            "VALUES(?, 'expense', ?, ?, ?, ?, 'daily', ?)",
            [
                (
                    expense.user_id,
                    amount_for_sql(expense.amount_cents),
                    expense.tx_date,
                    expense.target_category_id,
                    expense.note,
                    expense.created_at,
                )
                for expense in expenses
            ],
        )
        # These values are calculated from transactions. Clearing them makes
        # the Mini App rebuild every affected budget instead of using a value
        # computed before the recovered expense existed.
        user_ids = sorted({expense.user_id for expense in expenses})
        placeholders = ",".join("?" for _ in user_ids)
        target.execute(
            f"DELETE FROM reserve_movements WHERE source='auto' "
            f"AND user_id IN ({placeholders})",
            user_ids,
        )
    except Exception:
        target.rollback()
        raise
    else:
        target.commit()


def print_plan(expenses: list[Expense], stats: Counter, apply: bool) -> None:
    print(f"Проверено обычных трат: {stats['reviewed']}")
    print(f"Уже есть в общей базе: {stats['already_present']}")
    if stats["skipped_unknown_user"]:
        print(f"Пропущено: не найден пользователь: {stats['skipped_unknown_user']}")
    if stats["skipped_unknown_category"]:
        print(f"Пропущено: не найдена категория: {stats['skipped_unknown_category']}")
    print(f"Найдено к переносу: {len(expenses)}")
    for expense in expenses:
        category = expense.category_title or "Без категории"
        note = f" — {expense.note}" if expense.note else ""
        print(
            f"  • #{expense.source_id}: {expense.tx_date}, {category}, "
            f"{money(expense.amount_cents)} ₽{note}"
        )
    if not apply:
        print("Проверка завершена: общая база не изменена. Для переноса добавьте --apply.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        since = None
        if args.since:
            since = date.fromisoformat(args.since).isoformat()

        source_path = args.source.resolve()
        target_path = args.target.resolve()
        if source_path == target_path:
            raise ValueError("База-источник и общая база не должны совпадать")

        source = open_readonly(source_path)
        target = open_target(target_path)
        try:
            require_schema(source, "базе-источнике")
            require_schema(target, "общей базе")
            expenses, stats = build_recovery_plan(source, target, since)
            print_plan(expenses, stats, args.apply)
            if args.apply:
                apply_recovery(target, expenses)
                print(f"Готово: перенесено трат: {len(expenses)}")
        finally:
            source.close()
            target.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
