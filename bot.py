import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MINI_APP_URL = os.getenv("MINI_APP_URL", "")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not MINI_APP_URL:
        await update.message.reply_text("MINI_APP_URL не настроен на сервере.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Открыть бюджет", web_app=WebAppInfo(url=MINI_APP_URL))]
    ])
    await update.message.reply_text("Ваш личный бюджет:", reply_markup=keyboard)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
