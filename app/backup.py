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
from .db import connect, ensure_user


router = APIRouter(prefix="/api/backup", tags=["backup"])
BACKUP_VERSION = 8

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
    "buffer_account_balance",
)

TABLE_COLUMNS = {
    "categories": ("id", "title", "emoji", "sort_order", "created_at"),
    "income_rules": ("id", "title", "amount", "day_of_month", "kind", "is_payday", "active", "archived", "created_at"),
    "bill_rules": ("id", "title", "amount", "day_of_month", "category_id", "active", "archived", "created_at"),
    "transactions": (
        "id", "type", "amount", "tx_date", "category_id", "note",
        "bill_rule_id", "bill_due_date", "bill_planned_amount", "income_destination", "created_at",
    ),
    "vacations": ("id", "start_date", "end_date", "amount", "payment_date", "note", "created_at"),
    "reserve_movements": ("id", "period_start", "amount", "reason", "source", "created_at"),
    "piggy_bank_movements": (
        "id", "direction", "amount", "movement_date", "note", "source", "bill_payment_id", "income_transaction_id", "created_at"
    ),
    "buffer_account_movements": (
        "id", "direction", "amount", "movement_date", "note", "source", "income_transaction_id", "created_at"
    ),
    "cashflow_income_overrides": ("id", "period_start", "amount", "updated_at"),
    "plan_history": ("id", "effective_date", "snapshot"),
}


def _records(con: Any, table: str, columns: tuple[str, ...], user_id: int) -> list[dict]:
    selected = ",".join(columns)
    return [dict(row) for row in con.execute(f"SELECT {selected} FROM {table} WHERE user_id=? ORDER BY id", (user_id,))]


def export_user_data(user_id: int) -> dict:
    with connect() as con:
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
        with connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for table in ("buffer_account_movements", "piggy_bank_movements", "transactions", "cashflow_income_overrides", "plan_history", "reserve_movements", "vacations", "bill_rules", "income_rules", "categories"):
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
                # ``None`` deliberately keeps a restored pre-v8 backup in
                # its legacy virtual-buffer mode until its owner confirms the
                # real account amount.
                "buffer_account_balance": (
                    float(settings["buffer_account_balance"])
                    if settings.get("buffer_account_balance") is not None
                    else None
                ),
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
                    "INSERT INTO bill_rules(user_id,title,amount,day_of_month,category_id,active,archived,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        user_id, str(row["title"]), float(row.get("amount", 0)), int(row["day_of_month"]),
                        category_ids.get(row.get("category_id")), int(row["active"]), int(row["archived"]), _created_at(row),
                    ),
                )
                bill_ids[row.get("id")] = int(cur.lastrowid)

            transaction_ids: dict[Any, int] = {}
            for row in data["transactions"]:
                cur = con.execute(
                    "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note,bill_rule_id,bill_due_date,bill_planned_amount,income_destination,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, str(row["type"]), float(row["amount"]), str(row["tx_date"]),
                        category_ids.get(row.get("category_id")), str(row.get("note") or ""),
                        bill_ids.get(row.get("bill_rule_id")), row.get("bill_due_date"),
                        row.get("bill_planned_amount"), row.get("income_destination", "daily"), _created_at(row),
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
            for row in data["piggy_bank_movements"]:
                con.execute(
                    "INSERT INTO piggy_bank_movements"
                    "(user_id,direction,amount,movement_date,note,source,bill_payment_id,income_transaction_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, row['direction'], row['amount'], row['movement_date'], row['note'], row.get('source', 'external'),
                        transaction_ids.get(row.get('bill_payment_id')), transaction_ids.get(row.get('income_transaction_id')),
                        _created_at(row),
                    ),
                )
            for row in data["buffer_account_movements"]:
                con.execute(
                    "INSERT INTO buffer_account_movements"
                    "(user_id,direction,amount,movement_date,note,source,income_transaction_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        user_id,
                        row["direction"],
                        row["amount"],
                        row["movement_date"],
                        row.get("note") or "",
                        row.get("source") or "card_transfer",
                        transaction_ids.get(row.get("income_transaction_id")),
                        _created_at(row),
                    ),
                )
            for row in data["cashflow_income_overrides"]:
                con.execute(
                    "INSERT INTO cashflow_income_overrides"
                    "(user_id,period_start,amount,updated_at) VALUES(?,?,?,?)",
                    (
                        user_id, row["period_start"], row["amount"],
                        row.get("updated_at") or datetime.now(timezone.utc).isoformat(),
                    ),
                )
            for row in data["plan_history"]:
                snapshot = row['snapshot']
                for income in snapshot['income_rules']:
                    income['id'] = income_ids[income['id']]
                for bill in snapshot['bill_rules']:
                    bill['id'] = bill_ids[bill['id']]
                    bill['category_id'] = category_ids.get(bill.get('category_id'))
                con.execute("INSERT INTO plan_history(user_id,effective_date,snapshot) VALUES(?,?,?)",
                            (user_id, row['effective_date'], json.dumps(snapshot)))
    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        raise ValueError("Резервная копия повреждена или содержит неверные данные") from exc

    return {table: len(data[table]) for table in TABLE_COLUMNS}


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
