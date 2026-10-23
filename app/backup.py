from __future__ import annotations

import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from telegram import Bot, InputFile
from telegram.error import TelegramError

from .auth import TelegramUser, current_user
from .backup_validation import validate_backup
from .db import connect, ensure_user, user_scope


router = APIRouter(prefix="/api/backup", tags=["backup"])
BACKUP_VERSION = 12

SETTINGS_COLUMNS = (
    "currency",
    "initial_reserve",
    "initial_vacation_reserve",
    "forecast_months",
    "payroll_enabled",
    "salary_gross",
    "bonus_gross",
    "tax_rate",
    "salary_day",
    "advance_day",
    "cashflow_enabled",
    "cashflow_start_date",
    "cashflow_start_capital",
    "morning_report_time",
    "budget_mode",
    "irregular_reserve_percent",
    "irregular_reserve_target",
    "irregular_stretch_days",
    "irregular_bills_lookahead_days",
    "irregular_bills_scope",
    "irregular_start_date",
    "irregular_start_total",
    "irregular_start_reserve",
    "accounts_enabled",
    "main_account_id",
    "accounts_disabled_at",
    "buffer_floor",
)

TABLE_COLUMNS = {
    "categories": ("id", "title", "emoji", "sort_order", "created_at"),
    "income_rules": ("id", "title", "amount", "day_of_month", "kind", "is_payday", "active", "archived", "created_at"),
    "bill_rules": (
        "id", "title", "amount", "day_of_month", "category_id", "active", "archived",
        "once_date", "credit_account_id", "month_of_year", "created_at",
    ),
    "transactions": (
        "id", "type", "amount", "tx_date", "category_id", "note",
        "bill_rule_id", "bill_due_date", "bill_planned_amount", "income_destination",
        "reserve_percent", "income_source", "account_id", "funding_envelope", "created_at",
    ),
    "vacations": ("id", "start_date", "end_date", "amount", "payment_date", "note", "created_at"),
    "reserve_movements": ("id", "period_start", "amount", "reason", "source", "created_at"),
    "piggy_bank_movements": (
        "id", "direction", "amount", "movement_date", "note", "source", "purpose",
        "bill_payment_id", "income_transaction_id", "expense_transaction_id", "created_at",
    ),
    "cashflow_income_overrides": ("id", "period_start", "amount", "confirmed", "updated_at"),
    "plan_history": ("id", "effective_date", "snapshot"),
    "card_allocations": ("id", "period_start", "funded_on", "amount", "cap_kept"),
    "payroll_changes": ("id", "effective_month", "salary_gross", "bonus_gross", "created_at"),
    # «Максимум в день» (v12).
    "daily_caps": ("id", "effective_from", "amount", "topup", "excess_to", "moved", "created_at"),
    "emergency_reserve_movements": (
        "id", "direction", "amount", "movement_date", "reason", "kind", "expense_transaction_id", "created_at",
    ),
    # Cards and accounts (v11).
    "accounts": (
        "id", "title", "emoji", "kind", "opening_balance", "opening_date", "credit_limit", "payment_day",
        "sort_order", "archived", "created_at",
    ),
    "envelope_accounts": ("envelope", "account_id", "assigned_on", "assigned_at"),
    "account_transfers": (
        "id", "from_account_id", "to_account_id", "amount", "date", "note", "kind", "funding_envelope",
        "interest_amount", "piggy_movement_id", "reserve_movement_id", "transaction_id",
        "interest_transaction_id", "created_at",
    ),
    "account_adjustments": ("id", "account_id", "amount", "date", "reason", "note", "transaction_id", "created_at"),
    "account_incomes": (
        "id", "account_id", "amount", "date", "source", "payroll_period_start", "piggy_movement_id",
        "reserve_movement_id", "note", "created_at",
    ),
}


def _records(con: Any, table: str, columns: tuple[str, ...], user_id: int) -> list[dict]:
    selected = ",".join(columns)
    return [dict(row) for row in con.execute(f"SELECT {selected} FROM {table} WHERE user_id=? ORDER BY rowid", (user_id,))]


def export_user_data(user_id: int) -> dict:
    with user_scope(user_id), connect() as con:
        con.execute("BEGIN")
        settings_row = con.execute(
            f"SELECT {','.join(SETTINGS_COLUMNS)} FROM settings WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if settings_row is None:
            raise ValueError("Настройки пользователя не найдены")
        data = {"settings": dict(settings_row)}
        for table, columns in TABLE_COLUMNS.items():
            data[table] = _records(con, table, columns, user_id)

    return {
        "backup_version": BACKUP_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "app": "mimiapp-budget-bot",
        "data": data,
    }


def _validated_data(payload: dict) -> dict:
    return validate_backup(payload)


def _created_at(row: dict) -> str:
    value = row.get("created_at")
    return str(value) if value else datetime.now(timezone.utc).isoformat()


def restore_user_data(user_id: int, payload: dict) -> dict:
    data = _validated_data(payload)
    settings = data["settings"]

    try:
        with user_scope(user_id), connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for table in ("account_transfers", "account_adjustments", "account_incomes", "envelope_accounts",
                          "emergency_reserve_movements", "transactions", "piggy_bank_movements", "cashflow_income_overrides", "plan_history", "reserve_movements", "card_allocations", "daily_caps", "payroll_changes", "vacations", "bill_rules", "income_rules", "categories", "accounts"):
                con.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))

            # Versions before v5 stored the cash-flow start balance in
            # initial_reserve and a second, now obsolete, vacation field.
            legacy_start_capital = float(settings.get("initial_reserve", 0)) + float(
                settings.get("initial_vacation_reserve", 0)
            )
            values = {
                "currency": str(settings.get("currency", "RUB"))[:6].upper(),
                "initial_reserve": float(settings.get("initial_reserve", 0)),
                "initial_vacation_reserve": 0.0,
                "forecast_months": int(settings.get("forecast_months", 4)),
                "payroll_enabled": int(bool(settings.get("payroll_enabled", 0))),
                "salary_gross": float(settings.get("salary_gross", 0)),
                "bonus_gross": float(settings.get("bonus_gross", 0)),
                "tax_rate": float(settings.get("tax_rate", 13)),
                "salary_day": int(settings.get("salary_day", 7)),
                "advance_day": int(settings.get("advance_day", 22)),
                "cashflow_enabled": int(bool(settings.get("cashflow_enabled", 0))),
                "cashflow_start_date": settings.get("cashflow_start_date"),
                "cashflow_start_capital": float(
                    settings["cashflow_start_capital"]
                    if settings.get("cashflow_start_capital") is not None
                    else legacy_start_capital
                ),
                "morning_report_time": str(settings.get("morning_report_time") or "09:00"),
                # Versions before v10 had no irregular-income mode.
                "budget_mode": str(settings.get("budget_mode") or "payroll"),
                "irregular_reserve_percent": float(settings.get("irregular_reserve_percent", 10)),
                "irregular_reserve_target": settings.get("irregular_reserve_target"),
                "irregular_stretch_days": int(settings.get("irregular_stretch_days", 14)),
                "irregular_bills_lookahead_days": int(settings.get("irregular_bills_lookahead_days", 30)),
                "irregular_bills_scope": str(settings.get("irregular_bills_scope") or "month"),
                "irregular_start_date": settings.get("irregular_start_date"),
                "irregular_start_total": float(settings.get("irregular_start_total", 0)),
                "irregular_start_reserve": float(settings.get("irregular_start_reserve", 0)),
                # Cards and accounts: the main account is set once the accounts exist.
                "accounts_enabled": int(bool(settings.get("accounts_enabled", 0))),
                "main_account_id": None,
                "accounts_disabled_at": settings.get("accounts_disabled_at"),
                # «Минимум буфера»: absent in older backups.
                "buffer_floor": float(settings.get("buffer_floor") or 0),
            }
            assignments = ",".join(f"{column}=?" for column in SETTINGS_COLUMNS)
            con.execute(
                f"UPDATE settings SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (*[values[column] for column in SETTINGS_COLUMNS], user_id),
            )

            category_ids: dict[Any, int] = {}
            for position, row in enumerate(data["categories"], start=1):
                cur = con.execute(
                    "INSERT INTO categories(user_id,title,emoji,sort_order,created_at) VALUES(?,?,?,?,?)",
                    (
                        user_id, str(row["title"]), str(row.get("emoji") or "💳"),
                        int(row.get("sort_order") or position), _created_at(row),
                    ),
                )
                category_ids[row.get("id")] = int(cur.lastrowid)

            account_ids: dict[Any, int] = {}
            for row in data["accounts"]:
                cur = con.execute(
                    "INSERT INTO accounts(user_id,title,emoji,kind,opening_balance,opening_date,credit_limit,"
                    "payment_day,sort_order,archived,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, str(row["title"]), str(row.get("emoji") or "💳"), row["kind"],
                        float(row.get("opening_balance") or 0), str(row["opening_date"]), row.get("credit_limit"),
                        row.get("payment_day"), int(row.get("sort_order") or 0), int(bool(row.get("archived"))),
                        _created_at(row),
                    ),
                )
                account_ids[row["id"]] = int(cur.lastrowid)

            income_ids = {}
            for row in data["income_rules"]:
                cur = con.execute(
                    "INSERT INTO income_rules(user_id,title,amount,day_of_month,kind,is_payday,active,archived,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, str(row["title"]), float(row.get("amount", 0)), int(row["day_of_month"]),
                        str(row.get("kind") or "other"), int(bool(row.get("is_payday", 0))),
                        int(row["active"]), int(row["archived"]), _created_at(row),
                    ),
                )

                income_ids[row["id"]] = int(cur.lastrowid)

            bill_ids: dict[Any, int] = {}
            for row in data["bill_rules"]:
                cur = con.execute(
                    "INSERT INTO bill_rules(user_id,title,amount,day_of_month,category_id,active,archived,once_date,"
                    "credit_account_id,month_of_year,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, str(row["title"]), float(row.get("amount", 0)), int(row["day_of_month"]),
                        category_ids.get(row.get("category_id")), int(row["active"]), int(row["archived"]),
                        row.get("once_date"), account_ids.get(row.get("credit_account_id")), row.get("month_of_year"),
                        _created_at(row),
                    ),
                )
                bill_ids[row.get("id")] = int(cur.lastrowid)

            transaction_ids: dict[Any, int] = {}
            for row in data["transactions"]:
                cur = con.execute(
                    "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note,bill_rule_id,bill_due_date,"
                    "bill_planned_amount,income_destination,reserve_percent,income_source,account_id,funding_envelope,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, str(row["type"]), float(row["amount"]), str(row["tx_date"]),
                        category_ids.get(row.get("category_id")), str(row.get("note") or ""),
                        bill_ids.get(row.get("bill_rule_id")), row.get("bill_due_date"),
                        row.get("bill_planned_amount"), row.get("income_destination", "daily"),
                        row.get("reserve_percent"), str(row.get("income_source") or ""),
                        account_ids.get(row.get("account_id")), row.get("funding_envelope"), _created_at(row),
                    ),
                )
                transaction_ids[row.get("id")] = int(cur.lastrowid)

            for row in data["vacations"]:
                con.execute(
                    "INSERT INTO vacations(user_id,start_date,end_date,amount,payment_date,note,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        user_id, str(row["start_date"]), str(row["end_date"]), float(row["amount"]),
                        str(row["payment_date"]), str(row.get("note") or ""), _created_at(row),
                    ),
                )

            for row in data["reserve_movements"]:
                con.execute(
                    "INSERT INTO reserve_movements(user_id,period_start,amount,reason,source,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        user_id, str(row["period_start"]), float(row["amount"]), str(row.get("reason") or ""),
                        str(row.get("source") or "auto"), _created_at(row),
                    ),
                )
            piggy_ids: dict[Any, int] = {}
            cap_excess_days: dict[Any, int] = {}
            for row in data["piggy_bank_movements"]:
                if row.get('purpose') == 'cap_excess' and row['movement_date'] in cap_excess_days:
                    # A copy made while one day's excess was deposited twice:
                    # one deposit a day (the calculation sets its amount).
                    piggy_ids[row['id']] = cap_excess_days[row['movement_date']]
                    continue
                cur = con.execute(
                    "INSERT INTO piggy_bank_movements"
                    "(user_id,direction,amount,movement_date,note,source,purpose,bill_payment_id,income_transaction_id,"
                    "expense_transaction_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, row['direction'], row['amount'], row['movement_date'], row['note'], row.get('source', 'external'),
                        row.get('purpose'),
                        transaction_ids.get(row.get('bill_payment_id')), transaction_ids.get(row.get('income_transaction_id')),
                        transaction_ids.get(row.get('expense_transaction_id')), _created_at(row),
                    ),
                )
                piggy_ids[row['id']] = int(cur.lastrowid)
                if row.get('purpose') == 'cap_excess':
                    cap_excess_days[row['movement_date']] = int(cur.lastrowid)
            reserve_ids: dict[Any, int] = {}
            for row in data["emergency_reserve_movements"]:
                cur = con.execute(
                    "INSERT INTO emergency_reserve_movements"
                    "(user_id,direction,amount,movement_date,reason,kind,expense_transaction_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        user_id, row["direction"], row["amount"], row["movement_date"], row.get("reason") or "",
                        row["kind"], transaction_ids.get(row.get("expense_transaction_id")), _created_at(row),
                    ),
                )
                reserve_ids[row["id"]] = int(cur.lastrowid)
            for row in data["cashflow_income_overrides"]:
                con.execute(
                    "INSERT INTO cashflow_income_overrides"
                    "(user_id,period_start,amount,confirmed,updated_at) VALUES(?,?,?,?,?)",
                    (
                        user_id, row["period_start"], row["amount"], int(bool(row.get("confirmed"))),
                        row.get("updated_at") or datetime.now(timezone.utc).isoformat(),
                    ),
                )
            for row in data["card_allocations"]:
                con.execute(
                    "INSERT INTO card_allocations(user_id,period_start,funded_on,amount,cap_kept) VALUES(?,?,?,?,?)",
                    (user_id, row["period_start"], row["funded_on"], row["amount"], row.get("cap_kept") or 0),
                )
            for row in data["daily_caps"]:
                con.execute(
                    "INSERT INTO daily_caps(user_id,effective_from,amount,topup,excess_to,moved,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (user_id, row["effective_from"], row.get("amount"), row.get("topup") or 0,
                     row.get("excess_to") or "buffer", row.get("moved") or 0, _created_at(row)),
                )
            for row in data["payroll_changes"]:
                con.execute(
                    "INSERT INTO payroll_changes(user_id,effective_month,salary_gross,bonus_gross,created_at) VALUES(?,?,?,?,?)",
                    (user_id, row["effective_month"], float(row["salary_gross"]), float(row["bonus_gross"]), _created_at(row)),
                )
            for row in data["plan_history"]:
                snapshot = row['snapshot']
                for income in snapshot['income_rules']:
                    income['id'] = income_ids[income['id']]
                for bill in snapshot['bill_rules']:
                    bill['id'] = bill_ids[bill['id']]
                    bill['category_id'] = category_ids.get(bill.get('category_id'))
                    bill['credit_account_id'] = account_ids.get(bill.get('credit_account_id'))
                con.execute("INSERT INTO plan_history(user_id,effective_date,snapshot) VALUES(?,?,?)",
                            (user_id, row['effective_date'], json.dumps(snapshot)))
            _restore_accounts(con, user_id, data, account_ids, transaction_ids, piggy_ids, reserve_ids)
    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        raise ValueError("Резервная копия повреждена или содержит неверные данные") from exc

    return {table: len(data[table]) for table in TABLE_COLUMNS}


def _restore_accounts(con, user_id: int, data: dict, account_ids: dict, transaction_ids: dict,
                     piggy_ids: dict, reserve_ids: dict) -> None:
    """Cards and accounts with every reference mapped to the new ids."""
    for row in data["envelope_accounts"]:
        con.execute(
            "INSERT INTO envelope_accounts(user_id,envelope,account_id,assigned_on,assigned_at) VALUES(?,?,?,?,?)",
            (user_id, row["envelope"], account_ids[row["account_id"]], row["assigned_on"],
             row.get("assigned_at") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        )
    for row in data["account_transfers"]:
        con.execute(
            "INSERT INTO account_transfers(user_id,from_account_id,to_account_id,amount,date,note,kind,funding_envelope,"
            "interest_amount,piggy_movement_id,reserve_movement_id,transaction_id,interest_transaction_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, account_ids[row["from_account_id"]], account_ids.get(row.get("to_account_id")),
                float(row["amount"]), row["date"], row.get("note") or "", row["kind"], row.get("funding_envelope"),
                float(row.get("interest_amount") or 0), piggy_ids.get(row.get("piggy_movement_id")),
                reserve_ids.get(row.get("reserve_movement_id")), transaction_ids.get(row.get("transaction_id")),
                transaction_ids.get(row.get("interest_transaction_id")), _created_at(row),
            ),
        )
    for row in data["account_adjustments"]:
        con.execute(
            "INSERT INTO account_adjustments(user_id,account_id,amount,date,reason,note,transaction_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user_id, account_ids[row["account_id"]], float(row["amount"]), row["date"], row["reason"],
             row.get("note") or "", transaction_ids.get(row.get("transaction_id")), _created_at(row)),
        )
    for row in data["account_incomes"]:
        con.execute(
            "INSERT INTO account_incomes(user_id,account_id,amount,date,source,payroll_period_start,piggy_movement_id,"
            "reserve_movement_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, account_ids[row["account_id"]], float(row["amount"]), row["date"], row["source"],
             row.get("payroll_period_start"), piggy_ids.get(row.get("piggy_movement_id")),
             reserve_ids.get(row.get("reserve_movement_id")), row.get("note") or "", _created_at(row)),
        )
    main = data["settings"].get("main_account_id")
    con.execute("UPDATE settings SET main_account_id=? WHERE user_id=?", (account_ids.get(main), user_id))


@router.get("")
def download_backup(user: TelegramUser = Depends(current_user)) -> JSONResponse:
    ensure_user(user.id, user.first_name, user.username)
    try:
        payload = export_user_data(user.id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    filename = f"budget-backup-{datetime.now(timezone.utc).date().isoformat()}.json"
    return JSONResponse(
        payload,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/restore")
def upload_backup(payload: dict, user: TelegramUser = Depends(current_user)) -> dict:
    ensure_user(user.id, user.first_name, user.username)
    try:
        counts = restore_user_data(user.id, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "restored": counts}


@router.post("/send")
async def send_backup_to_chat(user: TelegramUser = Depends(current_user)) -> dict:
    ensure_user(user.id, user.first_name, user.username)
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise HTTPException(503, "BOT_TOKEN не настроен на сервере")

    try:
        payload = export_user_data(user.id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    filename = f"budget-backup-{datetime.now(timezone.utc).date().isoformat()}.json"
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    document = InputFile(io.BytesIO(content), filename=filename)
    try:
        async with Bot(token=token) as bot:
            message = await bot.send_document(
                chat_id=user.id,
                document=document,
                caption="Резервная копия бюджета. Сохраните этот файл — из него можно полностью восстановить данные.",
            )
    except TelegramError as exc:
        raise HTTPException(502, "Не удалось отправить файл в чат с ботом") from exc

    return {"ok": True, "message_id": message.message_id, "filename": filename}
