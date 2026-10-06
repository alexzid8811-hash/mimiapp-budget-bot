from __future__ import annotations

import io
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from openpyxl import Workbook
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.marker import DataPoint
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from telegram import Bot, InputFile
from telegram.error import TelegramError

from . import analytics, clock, irregular_engine
from .auth import TelegramUser, current_user
from .budget import add_months
from .db import connect, ensure_user, user_scope


router = APIRouter(prefix="/api/export", tags=["export"])
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="4F6BED")
TOTAL_FONT = Font(bold=True)
MONEY_FORMAT = "#,##0.00"
DATE_FORMAT = "DD.MM.YYYY"

INCOME_DESTINATIONS = {
    "daily": "Дневной бюджет", "buffer": "Буфер", "piggy": "Копилка",
    "split": "Разделить", "reserve": "Резерв на непредвиденное",
}
INCOME_KINDS = {"salary": "Зарплата", "advance": "Аванс", "other": "Другой доход"}


def _date(value: Any):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return str(value)


def _piggy_title(row: dict) -> str:
    if row["direction"] == "deposit":
        if row.get("bill_payment_id"):
            return "Остаток обязательного платежа"
        if row.get("income_transaction_id"):
            return "Доход в копилку"
        return "Из остатка дня" if row["source"] == "daily_budget" else "Пополнение"
    if row["source"] == "daily_budget":
        return "На карту · по дням периода" if row.get("purpose") == "transfer" else "На карту · на сегодня"
    return "Снятие"


def _write_table(
    ws: Worksheet,
    headers: list[str],
    rows: list[list[Any]],
    money_columns: set[int] = frozenset(),
    date_columns: set[int] = frozenset(),
    widths: list[int] | None = None,
) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in rows:
        ws.append(row)
    for index in range(1, len(headers) + 1):
        letter = get_column_letter(index)
        if index in money_columns:
            for cell in ws[letter][1:]:
                cell.number_format = MONEY_FORMAT
        if index in date_columns:
            for cell in ws[letter][1:]:
                cell.number_format = DATE_FORMAT
        width = widths[index - 1] if widths else max(12, len(headers[index - 1]) + 2)
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions


# Same colours as the analytics page, so the file reads like the app.
CATEGORY_COLORS = ("2A78D6", "EB6834", "1BAF7A", "EDA100", "E87BA4", "008300", "4A3AA7", "E34948")
OTHER_COLOR = "9AA1AD"
KIND_COLORS = {"daily": "2481CC", "bills": "8A6FD6", "piggy": "1F8F57"}
KIND_TITLES = {"daily": "Повседневные траты", "bills": "Обязательные платежи", "piggy": "Отложено в копилку"}
SECTION_FONT = Font(bold=True, size=13)
CHART_ROWS = 18


def _category_color(order: dict, category_id) -> str:
    index = order.get(category_id)
    return CATEGORY_COLORS[index] if index is not None and index < len(CATEGORY_COLORS) else OTHER_COLOR


def _fill(series, color: str) -> None:
    series.graphicalProperties.solidFill = color
    series.graphicalProperties.line.solidFill = color


def _header(ws: Worksheet, row: int, headers: list[str]) -> None:
    for column, title in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=column, value=title)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _rows(ws: Worksheet, row: int, values: list[list[Any]], money_columns: set[int]) -> int:
    for values_row in values:
        for column, value in enumerate(values_row, start=1):
            cell = ws.cell(row=row, column=column, value=value)
            if column in money_columns:
                cell.number_format = MONEY_FORMAT
        row += 1
    return row


def _bar_chart(title: str, *, horizontal: bool = False, stacked: bool = False, height: float = 8.5) -> BarChart:
    chart = BarChart()
    chart.type = "bar" if horizontal else "col"
    chart.title = title
    chart.style = 10
    chart.height, chart.width = height, 17
    chart.gapWidth = 40
    chart.y_axis.numFmt = "#,##0"
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    if stacked:
        chart.grouping = "stacked"
        chart.overlap = 100
    return chart


def build_charts_sheet(wb: Workbook, user_id: int, currency: str, today) -> None:
    """Sheet with the same charts as the analytics page: the current month
    and the last twelve months."""
    data = analytics.summary(user_id, "month", today, today)
    with connect() as con:
        order = {
            row["id"]: index
            for index, row in enumerate(con.execute(
                "SELECT id FROM categories WHERE user_id=? ORDER BY sort_order,id", (user_id,)
            ))
        }
    ws = wb.create_sheet("Графики", 1)
    ws.column_dimensions["A"].width = 26
    for letter in "BCDE":
        ws.column_dimensions[letter].width = 16
    ws["A1"] = f"Аналитика расходов · {data['label']}"
    ws["A1"].font = Font(bold=True, size=15)
    row = 3

    # 1. Where the money went.
    ws.cell(row=row, column=1, value="Куда ушли деньги").font = SECTION_FONT
    start = row + 1
    _header(ws, start, ["Направление", f"Сумма, {currency}", "Доля"])
    total = data["totals"]["total"]
    kinds = ["daily", "bills", "piggy"]
    end = _rows(ws, start + 1, [
        [KIND_TITLES[key], data["totals"][key], (data["totals"][key] / total) if total else 0]
        for key in kinds
    ], {2})
    for r in range(start + 1, end):
        ws.cell(row=r, column=3).number_format = "0%"
    ws.cell(row=end, column=1, value="Итого").font = TOTAL_FONT
    ws.cell(row=end, column=2, value=total).number_format = MONEY_FORMAT
    ws.cell(row=end, column=2).font = TOTAL_FONT
    if total:
        pie = PieChart()
        pie.title = "Куда ушли деньги"
        pie.height, pie.width = 8.5, 12
        pie.add_data(Reference(ws, min_col=2, min_row=start, max_row=end - 1), titles_from_data=True)
        pie.set_categories(Reference(ws, min_col=1, min_row=start + 1, max_row=end - 1))
        series = pie.series[0]
        for index, key in enumerate(kinds):
            point = DataPoint(idx=index)
            point.graphicalProperties.solidFill = KIND_COLORS[key]
            point.graphicalProperties.line.solidFill = "FFFFFF"
            series.dPt.append(point)
        pie.dataLabels = DataLabelList()
        pie.dataLabels.showPercent = True
        ws.add_chart(pie, f"G{row}")
    row = max(end + 2, row + CHART_ROWS)

    # 2. What it was spent on.
    ws.cell(row=row, column=1, value="На что потрачено (обычные траты)").font = SECTION_FONT
    start = row + 1
    _header(ws, start, ["Категория", f"Сумма, {currency}", "Доля", "Прошлый месяц"])
    cats = data["categories"]
    end = _rows(ws, start + 1, [
        [f"{c['emoji']} {c['title']}", c["amount"], c["share"], c["previous"]] for c in cats
    ], {2, 4})
    for r in range(start + 1, end):
        ws.cell(row=r, column=3).number_format = "0%"
    if cats:
        chart = _bar_chart("На что потрачено", horizontal=True, height=max(6, 1 + 0.8 * len(cats)))
        chart.add_data(Reference(ws, min_col=2, min_row=start, max_row=end - 1), titles_from_data=True)
        chart.set_categories(Reference(ws, min_col=1, min_row=start + 1, max_row=end - 1))
        chart.x_axis.scaling.orientation = "maxMin"  # largest on top, as in the app
        chart.legend = None
        series = chart.series[0]
        for index, c in enumerate(cats):
            point = DataPoint(idx=index)
            point.graphicalProperties.solidFill = _category_color(order, c["id"])
            point.graphicalProperties.line.solidFill = _category_color(order, c["id"])
            series.dPt.append(point)
        ws.add_chart(chart, f"G{row}")
    else:
        ws.cell(row=start + 1, column=1, value="Расходов за месяц нет")
    row = max(end + 2, row + max(CHART_ROWS, int(1 + 0.8 * len(cats) * 2)))

    # 3. By day.
    ws.cell(row=row, column=1, value="Траты по дням").font = SECTION_FONT
    start = row + 1
    _header(ws, start, ["Дата", f"Сумма, {currency}"])
    end = _rows(ws, start + 1, [[_date(d["date"]), d["amount"]] for d in data["days"]], {2})
    for r in range(start + 1, end):
        ws.cell(row=r, column=1).number_format = DATE_FORMAT
    chart = _bar_chart("Траты по дням")
    chart.add_data(Reference(ws, min_col=2, min_row=start, max_row=end - 1), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=start + 1, max_row=end - 1))
    chart.x_axis.number_format = "DD"
    chart.legend = None
    _fill(chart.series[0], KIND_COLORS["daily"])
    ws.add_chart(chart, f"G{row}")
    row = max(end + 2, row + CHART_ROWS)

    # 4. Last twelve months.
    ws.cell(row=row, column=1, value="По месяцам").font = SECTION_FONT
    start = row + 1
    _header(ws, start, ["Месяц", *(KIND_TITLES[key] for key in kinds)])
    months = analytics.monthly(user_id, add_months(today.replace(day=1), -11), 12)
    end = _rows(ws, start + 1, [
        [f"{m['label']} {m['month'][:4]}", *(m[key] for key in kinds)] for m in months
    ], {2, 3, 4})
    chart = _bar_chart("Куда ушли деньги по месяцам", stacked=True)
    chart.add_data(Reference(ws, min_col=2, max_col=4, min_row=start, max_row=end - 1), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=start + 1, max_row=end - 1))
    for series, key in zip(chart.series, kinds):
        _fill(series, KIND_COLORS[key])
    chart.legend.position = "b"
    ws.add_chart(chart, f"G{row}")


def _query(con: Any, sql: str, params: tuple) -> list[dict]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def build_workbook(user_id: int) -> bytes:
    with user_scope(user_id):
        return _build_workbook(user_id)


def _build_workbook(user_id: int) -> bytes:
    with connect() as con:
        con.execute("BEGIN")
        settings = con.execute("SELECT currency,budget_mode FROM settings WHERE user_id=?", (user_id,)).fetchone()
        if settings is None:
            raise ValueError("Настройки пользователя не найдены")
        transactions = _query(
            con,
            "SELECT t.*, c.title AS category_title, c.emoji AS category_emoji, b.title AS bill_title "
            "FROM transactions t LEFT JOIN categories c ON c.id=t.category_id AND c.user_id=t.user_id "
            "LEFT JOIN bill_rules b ON b.id=t.bill_rule_id AND b.user_id=t.user_id "
            "WHERE t.user_id=? ORDER BY t.tx_date,t.id",
            (user_id,),
        )
        piggy = _query(
            con,
            "SELECT * FROM piggy_bank_movements WHERE user_id=? ORDER BY movement_date,id",
            (user_id,),
        )
        bills = _query(
            con,
            "SELECT b.*, c.title AS category_title FROM bill_rules b "
            "LEFT JOIN categories c ON c.id=b.category_id AND c.user_id=b.user_id "
            "WHERE b.user_id=? AND b.archived=0 ORDER BY b.day_of_month,b.id",
            (user_id,),
        )
        incomes = _query(
            con,
            "SELECT * FROM income_rules WHERE user_id=? AND archived=0 ORDER BY day_of_month,id",
            (user_id,),
        )
        vacations = _query(
            con, "SELECT * FROM vacations WHERE user_id=? ORDER BY start_date,id", (user_id,)
        )
        has_reserve = con.execute(
            "SELECT 1 FROM emergency_reserve_movements WHERE user_id=? LIMIT 1", (user_id,)
        ).fetchone() is not None
        category_order = [
            row["title"] for row in con.execute(
                "SELECT title FROM categories WHERE user_id=? ORDER BY sort_order,id", (user_id,)
            )
        ]
    currency = settings["currency"] or "RUB"
    irregular = settings["budget_mode"] == "irregular"

    wb = Workbook()

    ws = wb.active
    ws.title = "Операции"
    op_rows = []
    for t in transactions:
        if t["bill_rule_id"]:
            kind, category = "Обязательный платёж", t["bill_title"] or t["category_title"] or ""
        elif t["type"] == "expense":
            kind, category = "Расход", t["category_title"] or "Без категории"
        else:
            kind, category = "Доход", INCOME_DESTINATIONS.get(t["income_destination"] or "daily", "")
        signed = -float(t["amount"]) if t["type"] == "expense" else float(t["amount"])
        op_rows.append([_date(t["tx_date"]), kind, category, signed, t["note"] or ""])
    _write_table(
        ws, ["Дата", "Тип", "Категория / назначение", f"Сумма, {currency}", "Комментарий"], op_rows,
        money_columns={4}, date_columns={1}, widths=[12, 22, 28, 16, 40],
    )

    ws = wb.create_sheet("Расходы по месяцам")
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in transactions:
        if t["type"] != "expense":
            continue
        category = "Обязательные платежи" if t["bill_rule_id"] else (t["category_title"] or "Без категории")
        totals[category][str(t["tx_date"])[:7]] += float(t["amount"])
    months = sorted({month for by_month in totals.values() for month in by_month})
    categories = sorted(totals, key=lambda name: -sum(totals[name].values()))
    pivot_rows = [
        [name, *[round(totals[name].get(month, 0.0), 2) or None for month in months],
         round(sum(totals[name].values()), 2)]
        for name in categories
    ]
    if pivot_rows:
        pivot_rows.append([
            "Итого",
            *[round(sum(totals[name].get(month, 0.0) for name in categories), 2) for month in months],
            round(sum(sum(v.values()) for v in totals.values()), 2),
        ])
    month_labels = [f"{m[5:7]}.{m[:4]}" for m in months]
    _write_table(
        ws, ["Категория", *month_labels, "Всего"], pivot_rows,
        money_columns=set(range(2, len(months) + 3)), widths=[28, *[14] * len(months), 16],
    )
    if pivot_rows:
        for cell in ws[ws.max_row]:
            cell.font = TOTAL_FONT
    if pivot_rows and months:
        # Stacked columns: one colour per category, months along the axis.
        chart = _bar_chart("Расходы по месяцам", stacked=True, height=10)
        chart.width = max(17, 3 + 2 * len(months))
        chart.add_data(
            Reference(ws, min_col=1, max_col=len(months) + 1, min_row=2, max_row=len(categories) + 1),
            from_rows=True, titles_from_data=True,
        )
        chart.set_categories(Reference(ws, min_col=2, max_col=len(months) + 1, min_row=1))
        order = {title: index for index, title in enumerate(category_order)}
        for series, name in zip(chart.series, categories):
            if name == "Обязательные платежи":
                color = KIND_COLORS["bills"]
            else:
                index = order.get(name)
                color = CATEGORY_COLORS[index] if index is not None and index < len(CATEGORY_COLORS) else OTHER_COLOR
            _fill(series, color)
        chart.legend.position = "b"
        ws.add_chart(chart, f"A{ws.max_row + 3}")

    build_charts_sheet(wb, user_id, currency, clock.today())

    ws = wb.create_sheet("Копилка")
    balance = 0.0
    piggy_rows = []
    for m in piggy:
        signed = float(m["amount"]) if m["direction"] == "deposit" else -float(m["amount"])
        balance = round(balance + signed, 2)
        piggy_rows.append([_date(m["movement_date"]), _piggy_title(m), signed, balance, m["note"] or ""])
    _write_table(
        ws, ["Дата", "Операция", f"Сумма, {currency}", "Остаток", "Комментарий"], piggy_rows,
        money_columns={3, 4}, date_columns={1}, widths=[12, 30, 16, 16, 40],
    )

    ws = wb.create_sheet("Обязательные платежи")
    _write_table(
        ws, ["Название", "День месяца", f"Сумма, {currency}", "Категория", "Активен"],
        [[b["title"], b["day_of_month"], float(b["amount"]), b["category_title"] or "", "Да" if b["active"] else "Нет"]
         for b in bills],
        money_columns={3}, widths=[30, 14, 16, 24, 10],
    )

    if irregular:
        _irregular_incomes_sheet(wb, user_id, transactions, currency)
    else:
        ws = wb.create_sheet("Доходы")
        _write_table(
            ws, ["Название", "Вид", "День месяца", f"Сумма, {currency}", "Активен"],
            [[i["title"], INCOME_KINDS.get(i["kind"], i["kind"]), i["day_of_month"], float(i["amount"]),
              "Да" if i["active"] else "Нет"] for i in incomes],
            money_columns={4}, widths=[30, 16, 14, 16, 10],
        )
    if irregular or has_reserve:
        _emergency_reserve_sheet(wb, user_id, currency)

    if vacations:
        ws = wb.create_sheet("Отпуска")
        _write_table(
            ws, ["Начало", "Окончание", "Дата выплаты", f"Отпускные, {currency}", "Комментарий"],
            [[_date(v["start_date"]), _date(v["end_date"]), _date(v["payment_date"]), float(v["amount"]), v["note"] or ""]
             for v in vacations],
            money_columns={4}, date_columns={1, 2, 3}, widths=[12, 12, 14, 18, 40],
        )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _irregular_incomes_sheet(wb: Workbook, user_id: int, transactions: list[dict], currency: str) -> None:
    """Actual incomes of the irregular mode with their split."""
    ran = irregular_engine.run(user_id)
    splits = ran[1].splits if ran else {}
    rows = []
    for t in transactions:
        if t["type"] != "income":
            continue
        split = splits.get(t["id"])
        parts = [split.reserve / 100, split.bills / 100, split.free / 100, split.piggy / 100] if split else [None] * 4
        rows.append([
            _date(t["tx_date"]), float(t["amount"]), t.get("income_source") or "",
            INCOME_DESTINATIONS.get(t["income_destination"] or "split", ""),
            t["reserve_percent"], *parts, t["note"] or "",
        ])
    ws = wb.create_sheet("Доходы")
    _write_table(
        ws, ["Дата", f"Сумма, {currency}", "Откуда", "Направление", "Процент в резерв", "В резерв",
             "На обязательные", "Свободные", "В копилку", "Комментарий"],
        rows, money_columns={2, 6, 7, 8, 9}, date_columns={1},
        widths=[12, 16, 22, 26, 12, 14, 16, 14, 14, 30],
    )


def _emergency_reserve_sheet(wb: Workbook, user_id: int, currency: str) -> None:
    movements = list(reversed(irregular_engine.reserve_snapshot(user_id)["movements"]))
    ws = wb.create_sheet("Резерв на непредвиденное")
    _write_table(
        ws, ["Дата", "Операция", f"Сумма, {currency}", "Остаток", "Причина"],
        [[_date(m["date"]), m["title"], m["amount"], m["balance"], m["reason"] or ""] for m in movements],
        money_columns={3, 4}, date_columns={1}, widths=[12, 30, 16, 16, 40],
    )


def _filename() -> str:
    return f"budget-{datetime.now(timezone.utc).date().isoformat()}.xlsx"


def _workbook_for(user: TelegramUser) -> bytes:
    ensure_user(user.id, user.first_name, user.username)
    try:
        return build_workbook(user.id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/xlsx")
def download_xlsx(user: TelegramUser = Depends(current_user)) -> Response:
    return Response(
        _workbook_for(user),
        media_type=XLSX_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{_filename()}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/xlsx/send")
async def send_xlsx_to_chat(user: TelegramUser = Depends(current_user)) -> dict:
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise HTTPException(503, "BOT_TOKEN не настроен на сервере")
    content = _workbook_for(user)
    filename = _filename()
    try:
        async with Bot(token=token) as bot:
            message = await bot.send_document(
                chat_id=user.id,
                document=InputFile(io.BytesIO(content), filename=filename),
                caption="Выгрузка бюджета в Excel: операции, графики расходов, расходы по месяцам, копилка и правила.",
            )
    except TelegramError as exc:
        raise HTTPException(502, "Не удалось отправить файл в чат с ботом") from exc
    return {"ok": True, "message_id": message.message_id, "filename": filename}
