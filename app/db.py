from __future__ import annotations

import os
import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL DEFAULT '',
    username TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    currency TEXT NOT NULL DEFAULT 'RUB',
    initial_reserve REAL NOT NULL DEFAULT 0,
    forecast_months INTEGER NOT NULL DEFAULT 4,
    payroll_enabled INTEGER NOT NULL DEFAULT 0,
    salary_gross REAL NOT NULL DEFAULT 0,
    bonus_gross REAL NOT NULL DEFAULT 0,
    tax_rate REAL NOT NULL DEFAULT 13,
    salary_day INTEGER NOT NULL DEFAULT 7,
    advance_day INTEGER NOT NULL DEFAULT 22,
    cashflow_enabled INTEGER NOT NULL DEFAULT 0,
    cashflow_start_date TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS income_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    amount REAL NOT NULL DEFAULT 0,
    day_of_month INTEGER NOT NULL CHECK(day_of_month BETWEEN 1 AND 31),
    kind TEXT NOT NULL DEFAULT 'other',
    is_payday INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bill_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    amount REAL NOT NULL DEFAULT 0,
    day_of_month INTEGER NOT NULL CHECK(day_of_month BETWEEN 1 AND 31),
    category_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '💳',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, title)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK(type IN ('expense','income')),
    amount REAL NOT NULL CHECK(amount >= 0),
    tx_date TEXT NOT NULL,
    category_id INTEGER,
    note TEXT NOT NULL DEFAULT '',
    bill_rule_id INTEGER,
    bill_due_date TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL,
    FOREIGN KEY(bill_rule_id) REFERENCES bill_rules(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_bill_payment
ON transactions(user_id, bill_rule_id, bill_due_date)
WHERE bill_rule_id IS NOT NULL AND bill_due_date IS NOT NULL;

CREATE TABLE IF NOT EXISTS vacations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount >= 0),
    payment_date TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(end_date >= start_date)
);

CREATE INDEX IF NOT EXISTS ix_vacations_user_dates
ON vacations(user_id, start_date, end_date, payment_date);

CREATE TABLE IF NOT EXISTS reserve_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    period_start TEXT NOT NULL,
    amount REAL NOT NULL,
    reason TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_auto_reserve_period
ON reserve_movements(user_id, period_start, source)
WHERE source = 'auto';

CREATE TABLE IF NOT EXISTS piggy_bank_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    direction TEXT NOT NULL CHECK(direction IN ('deposit','withdraw')),
    amount REAL NOT NULL CHECK(amount > 0),
    movement_date TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_piggy_bank_user_date
ON piggy_bank_movements(user_id, movement_date DESC, id DESC);
"""


def db_path() -> Path:
    path = Path(os.getenv("DATABASE_PATH", "./data/budget.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _ensure_settings_columns(con: sqlite3.Connection) -> None:
    columns = {row[1] for row in con.execute("PRAGMA table_info(settings)").fetchall()}
    additions = {
        "payroll_enabled": "INTEGER NOT NULL DEFAULT 0",
        "salary_gross": "REAL NOT NULL DEFAULT 0",
        "bonus_gross": "REAL NOT NULL DEFAULT 0",
        "tax_rate": "REAL NOT NULL DEFAULT 13",
        "salary_day": "INTEGER NOT NULL DEFAULT 7",
        "advance_day": "INTEGER NOT NULL DEFAULT 22",
        "cashflow_enabled": "INTEGER NOT NULL DEFAULT 0",
        "cashflow_start_date": "TEXT",
    }
    for name, ddl in additions.items():
        if name not in columns:
            con.execute(f"ALTER TABLE settings ADD COLUMN {name} {ddl}")


def init_db() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        _ensure_settings_columns(con)


def ensure_user(user_id: int, first_name: str = "", username: str | None = None) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO users(id, first_name, username) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username",
            (user_id, first_name, username),
        )
        con.execute("INSERT OR IGNORE INTO settings(user_id) VALUES(?)", (user_id,))

        count = con.execute("SELECT COUNT(*) FROM categories WHERE user_id=?", (user_id,)).fetchone()[0]
        if count == 0:
            for title, emoji in [
                ("Продукты", "🛒"), ("Транспорт", "🚕"), ("Кафе", "☕"),
                ("Дом", "🏠"), ("Здоровье", "💊"), ("Развлечения", "🎬"),
                ("Покупки", "🛍️"), ("Другое", "💳"),
            ]:
                con.execute("INSERT INTO categories(user_id,title,emoji) VALUES(?,?,?)", (user_id, title, emoji))

        income_count = con.execute("SELECT COUNT(*) FROM income_rules WHERE user_id=?", (user_id,)).fetchone()[0]
        if income_count == 0:
            con.execute(
                "INSERT INTO income_rules(user_id,title,amount,day_of_month,kind,is_payday) VALUES(?,?,?,?,?,1)",
                (user_id, "Зарплата", 0, 7, "salary"),
            )
            con.execute(
                "INSERT INTO income_rules(user_id,title,amount,day_of_month,kind,is_payday) VALUES(?,?,?,?,?,1)",
                (user_id, "Аванс", 0, 22, "advance"),
            )
