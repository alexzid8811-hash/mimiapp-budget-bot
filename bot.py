import asyncio
import logging
import os
from datetime import date
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import clock
from app.db import ensure_user, init_db
from app.expenses import (
    categories_for_user,
    create_expense,
    delete_expense,
    expense_for_user,
    update_expense,
)
from app.money import amount as money_amount
from app.money import cents
from app.reminders import pending_bill_reminders, record_bill_reminder
from app.morning_reports import (
    build_morning_report,
    pending_morning_reports,
    record_morning_report,
    resolve_morning_report,
)


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "")
REMINDER_CHECK_SECONDS = max(60, int(os.getenv("REMINDER_CHECK_SECONDS", "60")))
logger = logging.getLogger(__name__)
EXPENSE_DRAFT_KEY = "expense_draft"
EXPENSE_EDIT_KEY = "expense_edit"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    buttons = [[InlineKeyboardButton("➕ Добавить трату", callback_data="expense:start")]]
    if MINI_APP_URL:
        buttons.append([InlineKeyboardButton("💰 Открыть бюджет", web_app=WebAppInfo(url=MINI_APP_URL))])
    await update.effective_message.reply_text(
        "Ваш личный бюджет:", reply_markup=InlineKeyboardMarkup(buttons)
    )


def money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def ensure_telegram_user(update: Update) -> int | None:
    user = update.effective_user
    if user is None:
        return None
    ensure_user(user.id, user.first_name or "", user.username)
    return user.id


def expense_category_keyboard(categories: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for category in categories:
        title = f"{category['emoji']} {category['title']}"
        row.append(
            InlineKeyboardButton(title, callback_data=f"expense:category:{category['id']}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Отмена", callback_data="expense:cancel")])
    return InlineKeyboardMarkup(rows)


def parse_expense_amount(raw: str) -> float:
    """Accept common Russian money formats, for example ``1 250,50``."""
    normalized = raw.strip().lower().replace("₽", "").replace("руб.", "").replace("руб", "")
    normalized = "".join(normalized.replace("\u00a0", " ").split()).replace(",", ".")
    if not normalized or normalized.count(".") > 1 or any(char not in "0123456789." for char in normalized):
        raise ValueError
    try:
        parsed = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError
    result = money_amount(cents(parsed))
    if result <= 0:
        raise ValueError
    return result


def expense_actions_keyboard(transaction_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ Добавить ещё", callback_data="expense:start")],
        [InlineKeyboardButton("↩️ Отменить эту трату", callback_data=f"expense:undo:{transaction_id}")],
        [InlineKeyboardButton("✏️ Редактировать эту трату", callback_data=f"expense:edit:{transaction_id}")],
    ]
    if MINI_APP_URL:
        rows.append([InlineKeyboardButton("💰 Открыть бюджет", web_app=WebAppInfo(url=MINI_APP_URL))])
    return InlineKeyboardMarkup(rows)


def expense_text(transaction: dict, heading: str = "✅ Трата добавлена и уже учтена в бюджете.") -> str:
    category_title = transaction.get("category_title")
    category_emoji = transaction.get("category_emoji") or "💳"
    category = f"{category_emoji} {category_title}" if category_title else "💳 Без категории"
    try:
        tx_date = date.fromisoformat(str(transaction["tx_date"])).strftime("%d.%m.%Y")
    except (KeyError, TypeError, ValueError):
        tx_date = str(transaction.get("tx_date", ""))
    lines = [heading, "", f"{category} — {money(transaction['amount'])} ₽", f"Дата: {tx_date}"]
    if transaction.get("note"):
        lines.append(f"Комментарий: {transaction['note']}")
    return "\n".join(lines)


async def show_expense_actions(
    update: Update,
    transaction: dict,
    *,
    heading: str = "✅ Трата добавлена и уже учтена в бюджете.",
    query=None,
) -> None:
    text = expense_text(transaction, heading)
    keyboard = expense_actions_keyboard(int(transaction["id"]))
    if query is not None:
        await query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def start_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = ensure_telegram_user(update)
    message = update.effective_message
    if user_id is None or message is None:
        return
    categories = categories_for_user(user_id)
    if not categories:
        await message.reply_text("Сначала добавьте хотя бы одну категорию в бюджете.")
        return
    context.user_data.pop(EXPENSE_DRAFT_KEY, None)
    context.user_data.pop(EXPENSE_EDIT_KEY, None)
    prompt = "➕ Новая трата\n\nВыберите категорию:"
    query = update.callback_query
    if query is not None:
        await query.answer()
        await query.edit_message_text(prompt, reply_markup=expense_category_keyboard(categories))
    else:
        await message.reply_text(prompt, reply_markup=expense_category_keyboard(categories))


async def save_expense_from_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    query=None,
) -> None:
    user_id = ensure_telegram_user(update)
    draft = context.user_data.get(EXPENSE_DRAFT_KEY)
    if user_id is None or not draft:
        if query is not None:
            await query.answer("Начните ввод траты заново.", show_alert=True)
        return

    try:
        transaction = create_expense(
            user_id,
            draft["amount"],
            draft["category_id"],
            tx_date=draft["tx_date"],
            note=draft.get("note", ""),
        )
    except ValueError as exc:
        context.user_data.pop(EXPENSE_DRAFT_KEY, None)
        text = f"Не удалось добавить трату: {exc}. Начните ввод заново."
        if query is not None:
            await query.edit_message_text(text)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(text)
        return

    context.user_data.pop(EXPENSE_DRAFT_KEY, None)
    await show_expense_actions(update, transaction, query=query)


def expense_editor_keyboard(transaction: dict) -> InlineKeyboardMarkup:
    transaction_id = int(transaction["id"])
    rows = [
        [
            InlineKeyboardButton("Категорию", callback_data=f"expense:edit-category:{transaction_id}"),
            InlineKeyboardButton("Сумму", callback_data=f"expense:edit-amount:{transaction_id}"),
        ],
        [InlineKeyboardButton("Комментарий", callback_data=f"expense:edit-note:{transaction_id}")],
    ]
    if transaction.get("note"):
        rows.append([
            InlineKeyboardButton("Удалить комментарий", callback_data=f"expense:edit-clear-note:{transaction_id}")
        ])
    rows.append([InlineKeyboardButton("Готово", callback_data=f"expense:edit-done:{transaction_id}")])
    return InlineKeyboardMarkup(rows)


def edit_category_keyboard(transaction_id: int, categories: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for category in categories:
        row.append(InlineKeyboardButton(
            f"{category['emoji']} {category['title']}",
            callback_data=f"expense:edit-category-set:{transaction_id}:{category['id']}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Назад", callback_data=f"expense:edit:{transaction_id}")])
    return InlineKeyboardMarkup(rows)


async def show_expense_editor(
    update: Update, context: ContextTypes.DEFAULT_TYPE, transaction_id: int
) -> None:
    query = update.callback_query
    user_id = ensure_telegram_user(update)
    if query is None or user_id is None:
        return
    transaction = expense_for_user(user_id, transaction_id)
    if transaction is None:
        await query.answer("Трата не найдена.", show_alert=True)
        return
    context.user_data.pop(EXPENSE_EDIT_KEY, None)
    await query.answer()
    await query.edit_message_text(
        expense_text(transaction, "✏️ Редактирование траты"),
        reply_markup=expense_editor_keyboard(transaction),
    )


async def save_expense_edit_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    draft = context.user_data.get(EXPENSE_EDIT_KEY)
    user_id = ensure_telegram_user(update)
    if message is None or not message.text or not draft or user_id is None:
        return

    try:
        if draft["field"] == "amount":
            transaction = update_expense(
                user_id, int(draft["transaction_id"]), amount=parse_expense_amount(message.text)
            )
        else:
            note = message.text.strip()
            if len(note) > 200:
                await message.reply_text("Комментарий не должен быть длиннее 200 символов.")
                return
            transaction = update_expense(
                user_id, int(draft["transaction_id"]), note=note
            )
    except ValueError as exc:
        await message.reply_text(f"Не удалось изменить трату: {exc}.")
        return

    context.user_data.pop(EXPENSE_EDIT_KEY, None)
    await show_expense_actions(update, transaction, heading="✅ Трата изменена и бюджет пересчитан.")


async def handle_expense_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    data = query.data
    if data == "expense:start":
        await start_expense(update, context)
        return
    if data == "expense:cancel":
        context.user_data.pop(EXPENSE_DRAFT_KEY, None)
        context.user_data.pop(EXPENSE_EDIT_KEY, None)
        await query.answer("Ввод отменён")
        await query.edit_message_text("Ввод траты отменён.")
        return
    if data == "expense:skip-note":
        draft = context.user_data.get(EXPENSE_DRAFT_KEY)
        if not draft or draft.get("step") != "note":
            await query.answer("Сначала выберите категорию и сумму.", show_alert=True)
            return
        draft["note"] = ""
        await query.answer()
        await save_expense_from_draft(update, context, query=query)
        return
    if data.startswith("expense:undo:"):
        try:
            transaction_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная операция.", show_alert=True)
            return
        user_id = ensure_telegram_user(update)
        if user_id is None or not delete_expense(user_id, transaction_id):
            await query.answer("Эту трату уже нельзя отменить.", show_alert=True)
            return
        await query.answer("Трата отменена")
        await query.edit_message_text("↩️ Трата отменена и бюджет пересчитан.")
        return
    if data.startswith("expense:edit-category-set:"):
        parts = data.split(":")
        try:
            transaction_id, category_id = int(parts[2]), int(parts[3])
        except (IndexError, ValueError):
            await query.answer("Некорректная категория.", show_alert=True)
            return
        user_id = ensure_telegram_user(update)
        if user_id is None:
            return
        try:
            transaction = update_expense(user_id, transaction_id, category_id=category_id)
        except ValueError as exc:
            await query.answer(f"Не удалось изменить трату: {exc}.", show_alert=True)
            return
        context.user_data.pop(EXPENSE_EDIT_KEY, None)
        await query.answer()
        await show_expense_actions(
            update, transaction, heading="✅ Трата изменена и бюджет пересчитан.", query=query
        )
        return
    if data.startswith("expense:edit-category:"):
        try:
            transaction_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная трата.", show_alert=True)
            return
        user_id = ensure_telegram_user(update)
        if user_id is None or expense_for_user(user_id, transaction_id) is None:
            await query.answer("Трата не найдена.", show_alert=True)
            return
        categories = categories_for_user(user_id)
        await query.answer()
        await query.edit_message_text(
            "Выберите новую категорию:",
            reply_markup=edit_category_keyboard(transaction_id, categories),
        )
        return
    if data.startswith("expense:edit-amount:"):
        try:
            transaction_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная трата.", show_alert=True)
            return
        user_id = ensure_telegram_user(update)
        if user_id is None or expense_for_user(user_id, transaction_id) is None:
            await query.answer("Трата не найдена.", show_alert=True)
            return
        context.user_data[EXPENSE_EDIT_KEY] = {"transaction_id": transaction_id, "field": "amount"}
        await query.answer()
        await query.edit_message_text(
            "Введите новую сумму траты. Например: 350 или 1 250,50 ₽.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отмена", callback_data=f"expense:edit-cancel:{transaction_id}")]
            ]),
        )
        return
    if data.startswith("expense:edit-note:"):
        try:
            transaction_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная трата.", show_alert=True)
            return
        user_id = ensure_telegram_user(update)
        if user_id is None or expense_for_user(user_id, transaction_id) is None:
            await query.answer("Трата не найдена.", show_alert=True)
            return
        context.user_data[EXPENSE_EDIT_KEY] = {"transaction_id": transaction_id, "field": "note"}
        await query.answer()
        await query.edit_message_text(
            "Введите новый комментарий к трате.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Отмена", callback_data=f"expense:edit-cancel:{transaction_id}")]
            ]),
        )
        return
    if data.startswith("expense:edit-clear-note:"):
        try:
            transaction_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная трата.", show_alert=True)
            return
        user_id = ensure_telegram_user(update)
        if user_id is None:
            return
        try:
            transaction = update_expense(user_id, transaction_id, note="")
        except ValueError as exc:
            await query.answer(f"Не удалось изменить трату: {exc}.", show_alert=True)
            return
        context.user_data.pop(EXPENSE_EDIT_KEY, None)
        await query.answer()
        await show_expense_actions(
            update, transaction, heading="✅ Трата изменена и бюджет пересчитан.", query=query
        )
        return
    if data.startswith("expense:edit-done:") or data.startswith("expense:edit-cancel:"):
        try:
            transaction_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная трата.", show_alert=True)
            return
        user_id = ensure_telegram_user(update)
        transaction = expense_for_user(user_id, transaction_id) if user_id is not None else None
        if transaction is None:
            await query.answer("Трата не найдена.", show_alert=True)
            return
        context.user_data.pop(EXPENSE_EDIT_KEY, None)
        await query.answer()
        await show_expense_actions(update, transaction, query=query)
        return
    if data.startswith("expense:edit:"):
        try:
            transaction_id = int(data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer("Некорректная трата.", show_alert=True)
            return
        await show_expense_editor(update, context, transaction_id)
        return
    if not data.startswith("expense:category:"):
        return

    try:
        category_id = int(data.rsplit(":", 1)[1])
    except ValueError:
        await query.answer("Некорректная категория.", show_alert=True)
        return
    user_id = ensure_telegram_user(update)
    if user_id is None:
        return
    category = next((item for item in categories_for_user(user_id) if item["id"] == category_id), None)
    if category is None:
        await query.answer("Категория больше недоступна. Выберите другую.", show_alert=True)
        return
    context.user_data[EXPENSE_DRAFT_KEY] = {
        "step": "amount",
        "category_id": category_id,
        "category_title": category["title"],
        "category_emoji": category["emoji"],
        "tx_date": clock.today(),
    }
    await query.answer()
    await query.edit_message_text(
        f"{category['emoji']} {category['title']}\n\nВведите сумму траты. Например: 350 или 1 250,50 ₽.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="expense:cancel")]
        ]),
    )


async def handle_expense_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if context.user_data.get(EXPENSE_EDIT_KEY):
        await save_expense_edit_from_text(update, context)
        return

    draft = context.user_data.get(EXPENSE_DRAFT_KEY)
    if message is None or not draft or not message.text:
        return

    if draft.get("step") == "amount":
        try:
            draft["amount"] = parse_expense_amount(message.text)
        except ValueError:
            await message.reply_text("Введите сумму больше нуля, например: 350 или 1 250,50 ₽.")
            return
        draft["step"] = "note"
        await message.reply_text(
            f"Сумма: {money(draft['amount'])} ₽\n\n"
            "Добавьте комментарий или сохраните трату без него.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Без комментария", callback_data="expense:skip-note")],
                [InlineKeyboardButton("Отмена", callback_data="expense:cancel")],
            ]),
        )
        return

    if draft.get("step") == "note":
        note = message.text.strip()
        if len(note) > 200:
            await message.reply_text("Комментарий не должен быть длиннее 200 символов.")
            return
        draft["note"] = note
        await save_expense_from_draft(update, context)


async def cancel_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    cancelled = context.user_data.pop(EXPENSE_DRAFT_KEY, None)
    cancelled = context.user_data.pop(EXPENSE_EDIT_KEY, None) or cancelled
    if cancelled:
        await update.effective_message.reply_text("Ввод траты отменён.")
    else:
        await update.effective_message.reply_text("Сейчас нет незавершённой траты.")


def reminder_text(reminder: dict) -> str:
    if reminder["days_left"] == 0:
        when = "сегодня"
    elif reminder["days_left"] == 1:
        when = "завтра"
    else:
        when = f"через {reminder['days_left']} дн."
    return (
        "🔔 Напоминание об обязательном платеже\n\n"
        f"{reminder['title']} — {money(reminder['amount'])} ₽\n"
        f"Срок: {reminder['due_date'].strftime('%d.%m.%Y')} ({when})."
    )


async def send_bill_reminders(application) -> None:
    now = clock.now()
    current_time = now.strftime("%H:%M")
    for reminder in pending_bill_reminders(now.date()):
        # Using >= keeps reminders reliable after a short bot restart or a
        # delayed polling cycle.
        if current_time < reminder["reminder_time"]:
            continue
        try:
            await application.bot.send_message(
                chat_id=reminder["user_id"], text=reminder_text(reminder)
            )
        except Exception:
            # Keep it pending: a temporary Telegram error must not lose a
            # financial reminder. The next hourly check will retry it.
            logger.exception(
                "Не удалось отправить напоминание пользователю %s",
                reminder["user_id"],
            )
            continue
        record_bill_reminder(reminder)


def morning_report_keyboard(report: dict) -> InlineKeyboardMarkup | None:
    if report.get("unused_amount", 0) <= 0 or report.get("decision") is not None:
        return None
    report_date = report["report_date"].isoformat()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🐷 Перевести в копилку",
            callback_data=f"morning:piggy:{report_date}",
        )],
        [InlineKeyboardButton(
            "📅 Распределить на дни",
            callback_data=f"morning:daily:{report_date}",
        )],
    ])


def morning_report_text(report: dict) -> str:
    expenses = "\n".join(f"• {item['title']} — {money(item['amount'])} ₽" for item in report["expenses"])
    spending = f"Вчера потрачено: {money(report['spent_total'])} ₽"
    if expenses:
        spending += "\n" + expenses
    else:
        spending += "\nВчера расходов не было."
    change = "нет данных за предыдущий день" if report["daily_change"] is None else (
        ("+" if report["daily_change"] >= 0 else "−") + money(abs(report["daily_change"])) + " ₽"
    )
    lines = [
        f"☀️ Доброе утро! Итоги за {report['yesterday'].strftime('%d.%m.%Y')}",
        "", spending,
    ]
    if report.get("unused_amount", 0) > 0:
        lines.extend(["", f"Неиспользованный остаток: {money(report['unused_amount'])} ₽"])
        if report.get("decision") is None:
            lines.append("Выберите, что с ним сделать:")
        elif report["decision"] == "piggy":
            lines.append("✅ Остаток переведён в копилку.")
        elif report["decision"] == "daily":
            lines.append("✅ Остаток распределён на оставшиеся дни периода.")
    lines.extend([
        "",
        f"До конца периода: {report['period_days_left']} дн. · осталось {money(report['period_remaining'])} ₽",
        f"На карте на момент отчёта: {money(report['card_balance'])} ₽",
        f"На день сегодня: {money(report['daily_amount'])} ₽",
        f"Изменение дневной нормы: {change}",
        "", f"Буфер: {money(report['buffer_balance'])} ₽",
        f"Копилка: {money(report['piggy_balance'])} ₽",
    ])
    if report["nearest_due_date"] is None:
        lines.extend(["", "Ближайших обязательных платежей нет."])
    else:
        days = report["nearest_days_left"]
        when = "сегодня" if days == 0 else "завтра" if days == 1 else f"через {days} дн."
        lines.extend(["", f"Ближайшие обязательные платежи: {report['nearest_due_date'][8:10]}.{report['nearest_due_date'][5:7]}.{report['nearest_due_date'][:4]} ({when})"])
        lines.extend(f"• {bill['title']} — {money(bill['amount'])} ₽" for bill in report["nearest_bills"])
    return "\n".join(lines)


async def handle_morning_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "morning" or parts[1] not in {"piggy", "daily"}:
        return
    try:
        report_date = date.fromisoformat(parts[2])
    except ValueError:
        await query.answer("Некорректная дата отчёта.", show_alert=True)
        return

    applied = resolve_morning_report(query.from_user.id, report_date, parts[1])
    if not applied:
        await query.answer("Этот остаток уже обработан.", show_alert=True)
        return
    await query.answer()

    updated = build_morning_report(query.from_user.id, clock.now().date())
    await context.bot.send_message(
        chat_id=query.from_user.id,
        text=morning_report_text(updated),
    )
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Не удалось убрать кнопки из старого отчёта", exc_info=True)


async def send_morning_reports(application) -> None:
    for report in pending_morning_reports(clock.now()):
        try:
            await application.bot.send_message(
                chat_id=report["user_id"],
                text=morning_report_text(report),
                reply_markup=morning_report_keyboard(report),
            )
        except Exception:
            logger.exception(
                "Не удалось отправить утренний отчёт пользователю %s",
                report["user_id"],
            )
            continue
        record_morning_report(report)


async def reminder_loop(application) -> None:
    while True:
        await send_bill_reminders(application)
        await send_morning_reports(application)
        await asyncio.sleep(REMINDER_CHECK_SECONDS)


async def post_init(application) -> None:
    init_db()
    application.bot_data["reminder_task"] = asyncio.create_task(reminder_loop(application))


async def post_shutdown(application) -> None:
    task = application.bot_data.get("reminder_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("expense", start_expense))
    app.add_handler(CommandHandler("cancel", cancel_expense))
    app.add_handler(CallbackQueryHandler(handle_expense_callback, pattern=r"^expense:"))
    app.add_handler(CallbackQueryHandler(handle_morning_decision, pattern=r"^morning:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_expense_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
