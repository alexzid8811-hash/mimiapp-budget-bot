import asyncio
import logging
import os

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, ReplyKeyboardMarkup, Update, WebAppInfo,
)
from telegram.constants import ChatType
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from app import clock
from app.db import init_db, user_scope
from app.morning_reports import pending_morning_reports, record_morning_report
from app.quick_stats import is_irregular_user, quick_numbers


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "")
REMINDER_CHECK_SECONDS = max(60, int(os.getenv("REMINDER_CHECK_SECONDS", "60")))
logger = logging.getLogger(__name__)

BTN_TODAY = "📅 На сегодня"
BTN_SPENT = "🧾 Траты сегодня"
BTN_CARD = "💳 На карте"
BTN_FREE = "💰 Свободные деньги"
# Only on keyboards sent before the menu button «Бюджет» took its place.
BTN_OPEN = "📊 Открыть бюджет"
MENU_BUTTONS = [BTN_TODAY, BTN_SPENT, BTN_CARD, BTN_FREE, BTN_OPEN]
# The menu under the message field, one per budget mode: the irregular-income
# mode has no card, its spending money is the free money.  Text buttons only:
# a web_app button of this keyboard opens the mini app without initData, and
# the server rejects such a start (app/auth.py).  The app opens straight from
# the menu button «Бюджет» left of the message field (setup_menu_button).
MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_TODAY, BTN_SPENT], [BTN_CARD]], resize_keyboard=True, is_persistent=True,
)
IRREGULAR_MENU_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_TODAY, BTN_SPENT], [BTN_FREE]], resize_keyboard=True, is_persistent=True,
)


def menu_keyboard(irregular: bool) -> ReplyKeyboardMarkup:
    return IRREGULAR_MENU_KEYBOARD if irregular else MENU_KEYBOARD


def open_budget_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Открыть бюджет", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not MINI_APP_URL:
        await update.message.reply_text("MINI_APP_URL не настроен на сервере.")
        return
    await update.message.reply_text("Ваш личный бюджет:", reply_markup=open_budget_markup())
    await show_menu(update, context)


def _irregular_for(user_id: int) -> bool:
    with user_scope(user_id):
        return is_irregular_user(user_id)


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # The buttons work in the private chat only (see build_application).
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    user_id = update.effective_user.id
    try:
        irregular = await asyncio.to_thread(_irregular_for, user_id)
    except Exception:
        logger.exception("Не удалось узнать режим бюджета пользователя %s", user_id)
        irregular = False
    await update.effective_message.reply_text(
        "Быстрое меню — под полем ввода сообщения.", reply_markup=menu_keyboard(irregular)
    )


def money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _ddmm(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}"


PAYDAY_WAITING = "⏳ Выплата ещё не подтверждена. Когда деньги придут, нажмите «Получил ✓» в приложении."


def today_text(numbers: dict) -> str:
    lines = [
        f"📅 Сегодня, {_ddmm(numbers['today'])}", "",
        f"Можно потратить: {money(numbers['today_target'])} ₽",
        f"Потрачено: {money(numbers['spent_today'])} ₽",
        f"Осталось на сегодня: {money(numbers['available_today'])} ₽",
    ]
    if numbers["overspend"] > 0:
        lines.extend(["", f"⚠️ Перерасход {money(numbers['overspend'])} ₽. "
                          "Как его покрыть, можно выбрать в приложении."])
    if numbers.get("payday_waiting"):
        lines.extend(["", PAYDAY_WAITING])
    if numbers.get("no_income_warning"):
        lines.extend(["", f"⚠️ Дохода не было {numbers['days_without_income']} дн. "
                          "При необходимости возьмите деньги из резерва."])
    return "\n".join(lines)


# Keeps a reply far below Telegram's limit of 4096 characters.
MAX_ITEMS = 25


def _items(items: list[dict]) -> list[str]:
    lines = [f"• {item['title'][:60]} — {money(item['amount'])} ₽" for item in items[:MAX_ITEMS]]
    if len(items) > MAX_ITEMS:
        lines.append(f"… и ещё {len(items) - MAX_ITEMS} на {money(sum(i['amount'] for i in items[MAX_ITEMS:]))} ₽")
    return lines


def spent_text(numbers: dict) -> str:
    lines = [f"🧾 Траты за {_ddmm(numbers['today'])}", "", f"Потрачено: {money(numbers['spent_today'])} ₽"]
    lines.extend(_items(numbers["expenses"]))
    # The part of mandatory payments beyond the money put aside for them is
    # paid from today's money: an overpayment, or (irregular mode) a bill
    # whose money was not fully put aside.
    rest = round(numbers["spent_today"] - sum(item["amount"] for item in numbers["expenses"]), 2)
    if rest > 0:
        lines.append(f"• Обязательные платежи сверх отложенного — {money(rest)} ₽")
    if not (numbers["spent_today"] or numbers["expenses"] or numbers["bills_paid"] or numbers["reserve_paid"]):
        lines.append("Сегодня трат ещё не было.")
    if numbers["bills_paid"]:
        title = "Оплачены обязательные платежи" + (":" if rest > 0 else " (не из суммы на день):")
        lines.extend(["", title, *_items(numbers["bills_paid"])])
    if numbers["reserve_paid"]:
        lines.extend(["", "Оплачено из резерва на непредвиденное:", *_items(numbers["reserve_paid"])])
    return "\n".join(lines)


def card_text(numbers: dict) -> str:
    if numbers["mode"] == "irregular":
        # This mode has no card: its spending money is the free money.
        return "\n".join([
            f"💰 Свободных денег: {money(numbers['free_balance'])} ₽",
            f"Растягиваем до {_ddmm(numbers['stretch_until'])} (осталось {numbers['days_left']} дн.)",
        ])
    lines = [
        f"💳 На карте: {money(numbers['card_balance'])} ₽",
        f"На траты до {_ddmm(numbers['period_end'])} включительно (осталось {numbers['period_days_left']} дн.)",
    ]
    if numbers.get("payday_waiting"):
        lines.extend(["", PAYDAY_WAITING])
    return "\n".join(lines)


def menu_reply_text(button: str, numbers: dict | None) -> str:
    """Answer to one of the number buttons; None: the user has no budget yet."""
    if numbers is None:
        return "Бюджет ещё не настроен. Откройте его, и здесь появятся цифры."
    if not numbers["configured"]:
        return "Режим «Подработки» ещё не настроен. Откройте бюджет и укажите, с чего начать."
    if button == BTN_TODAY:
        return today_text(numbers)
    if button == BTN_SPENT:
        return spent_text(numbers)
    return card_text(numbers)


def _numbers_for(user_id: int) -> dict | None:
    with user_scope(user_id):
        return quick_numbers(user_id)


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user_id = update.effective_user.id
    if message.text == BTN_OPEN:
        # An old keyboard: open the budget from here once and replace the
        # keyboard with the new one, without this button.
        if MINI_APP_URL:
            await message.reply_text("Ваш личный бюджет:", reply_markup=open_budget_markup())
        try:
            irregular = await asyncio.to_thread(_irregular_for, user_id)
        except Exception:
            logger.exception("Не удалось узнать режим бюджета пользователя %s", user_id)
            irregular = False
        await message.reply_text(
            "Теперь бюджет открывается сразу кнопкой «Бюджет» слева от поля ввода.",
            reply_markup=menu_keyboard(irregular),
        )
        return
    try:
        # The calculation is synchronous: keep the event loop (and the
        # morning reports) free while it runs.
        numbers = await asyncio.to_thread(_numbers_for, user_id)
    except Exception:
        logger.exception("Не удалось посчитать бюджет пользователя %s", user_id)
        await message.reply_text("Не получилось посчитать бюджет. Попробуйте чуть позже.")
        return
    if numbers is None or not numbers["configured"]:
        markup = open_budget_markup() if MINI_APP_URL else None
    else:
        # After a switch of the budget mode the keyboard still shows the
        # other mode's button: answer it and bring the right keyboard.
        irregular = numbers["mode"] == "irregular"
        markup = menu_keyboard(irregular) if message.text == (BTN_CARD if irregular else BTN_FREE) else None
    await message.reply_text(menu_reply_text(message.text, numbers), reply_markup=markup)


def irregular_report_text(report: dict, spending: str, change: str) -> str:
    lines = [
        f"☀️ Доброе утро! Итоги за {report['yesterday'].strftime('%d.%m.%Y')}",
        "", spending, "",
        f"Можно сегодня: {money(report['daily_amount'])} ₽",
        f"Изменение дневной суммы: {change}",
        f"Свободных денег: {money(report['free_balance'])} ₽ · растягиваем до "
        f"{_ddmm(report['stretch_until'])} (осталось {report['days_left']} дн.)",
        "",
        f"Резерв на непредвиденное: {money(report['reserve_balance'])} ₽"
        + (f" · хватит примерно на {report['reserve_days']} дн." if report.get("reserve_days") else ""),
        f"Отложено на обязательные: {money(report['bills_reserved'])} ₽",
        f"Копилка: {money(report['piggy_balance'])} ₽",
    ]
    bill = report.get("next_bill")
    if bill:
        lines.extend(["", f"Ближайший обязательный платёж: {bill['title']} — {money(bill['amount'])} ₽, "
                          f"{_ddmm(bill['due_date'])} (отложено {money(bill['reserved'])} ₽)"])
    else:
        lines.extend(["", "Ближайших обязательных платежей нет."])
    shortfall = report.get("bills_shortfall")
    if shortfall:
        lines.append(f"⚠️ На обязательные не хватает {money(shortfall['amount'])} ₽ к {_ddmm(shortfall['date'])}")
    if report.get("no_income_warning"):
        lines.append(f"⚠️ Дохода не было {report['days_without_income']} дн. При необходимости возьмите деньги из резерва.")
    return "\n".join(lines)


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
    if report.get("mode") == "irregular":
        return irregular_report_text(report, spending, change)
    if report.get("resend"):
        title = "✅ Зарплата подтверждена — бюджет пересчитан"
    else:
        title = f"☀️ Доброе утро! Итоги за {report['yesterday'].strftime('%d.%m.%Y')}"
    lines = [title]
    if report.get("payday_waiting"):
        lines.extend(["", "⏳ Выплата ещё не подтверждена — суммы посчитаны по расчётной сумме. "
                          "Когда деньги придут, нажмите «Получил ✓» в приложении."])
    lines += [
        "", spending, "",
        f"До конца периода: {report['period_days_left']} дн. · осталось {money(report['period_remaining'])} ₽",
        f"На день сегодня: {money(report['daily_amount'])} ₽",
        f"Изменение дневной суммы: {change}",
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
            # The report also brings the quick menu to users who have not
            # pressed /start since it appeared.
            await application.bot.send_message(
                chat_id=report["user_id"], text=morning_report_text(report),
                reply_markup=menu_keyboard(report.get("mode") == "irregular"),
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
        await send_morning_reports(application)
        await asyncio.sleep(REMINDER_CHECK_SECONDS)


async def post_init(application) -> None:
    init_db()
    application.bot_data["reminder_task"] = asyncio.create_task(reminder_loop(application))
    await setup_menu_button(application.bot)


MENU_BUTTON_TEXT = "Бюджет"


async def setup_menu_button(bot) -> None:
    """The button left of the message field opens the mini app.  A web app
    button set by somebody else (for example in @BotFather) is kept as it is;
    the bot's own button follows a changed MINI_APP_URL."""
    if not MINI_APP_URL:
        return
    try:
        current = await bot.get_chat_menu_button()
        if isinstance(current, MenuButtonWebApp) and (
            current.text != MENU_BUTTON_TEXT or current.web_app.url == MINI_APP_URL
        ):
            return
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text=MENU_BUTTON_TEXT, web_app=WebAppInfo(url=MINI_APP_URL))
        )
    except Exception:
        logger.exception("Не удалось настроить кнопку меню")


async def post_shutdown(application) -> None:
    task = application.bot_data.get("reminder_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def build_application():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", show_menu))
    app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & filters.Text(MENU_BUTTONS), menu_button
    ))
    return app


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
