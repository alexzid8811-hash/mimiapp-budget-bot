import asyncio
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from app import clock
from app.db import init_db
from app.reminders import pending_bill_reminders, record_bill_reminder
from app.morning_reports import pending_morning_reports, record_morning_report


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "")
REMINDER_CHECK_SECONDS = max(60, int(os.getenv("REMINDER_CHECK_SECONDS", "60")))
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not MINI_APP_URL:
        await update.message.reply_text("MINI_APP_URL не настроен на сервере.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Открыть бюджет", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])
    await update.message.reply_text("Ваш личный бюджет:", reply_markup=keyboard)


def money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


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
        "", spending, "",
        f"До конца периода: {report['period_days_left']} дн. · осталось {money(report['period_remaining'])} ₽",
        f"На карте на момент отчёта: {money(report['card_balance'])} ₽",
        f"На день сегодня: {money(report['daily_amount'])} ₽",
        f"Изменение дневной нормы: {change}",
        "", f"Буфер: {money(report['buffer_balance'])} ₽",
        f"Копилка: {money(report['piggy_balance'])} ₽",
    ]
    if report["nearest_due_date"] is None:
        lines.extend(["", "Ближайших обязательных платежей нет."])
    else:
        days = report["nearest_days_left"]
        when = "сегодня" if days == 0 else "завтра" if days == 1 else f"через {days} дн."
        lines.extend(["", f"Ближайшие обязательные платежи: {report['nearest_due_date'][8:10]}.{report['nearest_due_date'][5:7]}.{report['nearest_due_date'][:4]} ({when})"])
        lines.extend(f"• {bill['title']} — {money(bill['amount'])} ₽" for bill in report["nearest_bills"])
    return "\n".join(lines)


async def send_morning_reports(application) -> None:
    for report in pending_morning_reports(clock.now()):
        try:
            await application.bot.send_message(chat_id=report["user_id"], text=morning_report_text(report))
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
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
