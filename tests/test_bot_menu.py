"""The quick menu of the bot: the same numbers as the home screen, and the
existing bot behaviour (/start, morning reports) kept as it was."""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from telegram import Chat, Message, MenuButtonCommands, MenuButtonWebApp, Update, User

import bot
from app import clock
from app.cashflow_app import app
from app.db import connect, ensure_user, init_db, user_db_path, user_scope
from app.quick_stats import quick_numbers


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setattr(clock, "today", lambda: date(2026, 9, 15))
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    init_db()
    ensure_user(1)
    with connect() as con:
        con.execute("UPDATE income_rules SET amount=10000 WHERE user_id=1 AND kind='salary'")
        con.execute("UPDATE settings SET cashflow_enabled=1,cashflow_start_date='2026-09-01',"
                    "cashflow_start_capital=30000,forecast_months=1 WHERE user_id=1")
    with TestClient(app) as value:
        yield value


def post(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def expense(client, amount, note, day="2026-09-15"):
    return post(client, "/api/transactions", {"type": "expense", "amount": amount, "tx_date": day, "note": note})


def press(button, user_id=1):
    """Run the button handler with a fake update; return its replies."""
    replies = []

    async def reply_text(text, reply_markup=None):
        replies.append((text, reply_markup))

    message = SimpleNamespace(text=button, reply_text=reply_text)
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=user_id))
    asyncio.run(bot.menu_button(update, None))
    return replies


def test_payroll_numbers_match_the_home_screen(client):
    bill = post(client, "/api/bill-rules", {
        "title": "Интернет", "amount": 600, "day_of_month": 15, "effective_date": "2026-09-01"})
    post(client, f"/api/bills/{bill['id']}/pay", {"due_date": "2026-09-15"})
    expense(client, 800, "Продукты")
    expense(client, 450, "Кафе")
    expense(client, 300, "Вчерашнее", day="2026-09-14")

    with user_scope(1):
        numbers = quick_numbers(1)
    dashboard = client.get("/api/dashboard").json()
    flow = client.get("/api/cashflow").json()
    assert numbers["mode"] == "payroll" and numbers["configured"]
    assert numbers["today_target"] == flow["today_target"]
    assert numbers["available_today"] == dashboard["daily_available"] == flow["available_today"]
    assert numbers["spent_today"] == dashboard["spent_today"] == 1250
    # «На карте» on the home screen is the spending money of the period.
    assert numbers["card_balance"] == dashboard["remaining"] == flow["remaining_period"]
    assert numbers["period_end"] == flow["period"]["end"]
    assert numbers["period_days_left"] == flow["period"]["days_left"]
    assert numbers["expenses"] == [{"title": "Продукты", "amount": 800.0}, {"title": "Кафе", "amount": 450.0}]
    assert numbers["bills_paid"] == [{"title": "Интернет", "amount": 600.0}]

    text = bot.today_text(numbers)
    assert f"Можно потратить: {bot.money(flow['today_target'])} ₽" in text
    assert "Потрачено: 1 250,00 ₽" in text
    assert f"Осталось на сегодня: {bot.money(flow['available_today'])} ₽" in text
    assert "Перерасход" not in text

    text = bot.spent_text(numbers)
    assert "Потрачено: 1 250,00 ₽\n• Продукты — 800,00 ₽\n• Кафе — 450,00 ₽" in text
    assert "Оплачены обязательные платежи (не из суммы на день):\n• Интернет — 600,00 ₽" in text
    assert "Вчерашнее" not in text and "Сверх плана" not in text

    text = bot.card_text(numbers)
    assert text.startswith(f"💳 На карте: {bot.money(flow['remaining_period'])} ₽")
    assert f"до {flow['period']['end'][8:10]}.{flow['period']['end'][5:7]} включительно" in text


def test_overspend_and_overpaid_bill_are_shown(client):
    bill = post(client, "/api/bill-rules", {
        "title": "Интернет", "amount": 600, "day_of_month": 15, "effective_date": "2026-09-01"})
    payment = post(client, f"/api/bills/{bill['id']}/pay", {"due_date": "2026-09-15"})
    response = client.put(f"/api/bill-payments/{payment['id']}", json={"amount": 700})
    assert response.status_code == 200, response.text
    expense(client, 50000, "Ноутбук")

    with user_scope(1):
        numbers = quick_numbers(1)
    # The overpayment of 100 ₽ is paid from the card, so it is spending.
    assert numbers["spent_today"] == client.get("/api/dashboard").json()["spent_today"] == 50100
    assert numbers["overspend"] > 0
    assert "⚠️ Перерасход" in bot.today_text(numbers)
    text = bot.spent_text(numbers)
    assert "• Ноутбук — 50 000,00 ₽\n• Сверх плана по обязательным платежам — 100,00 ₽" in text
    assert "• Интернет — 700,00 ₽" in text


def test_no_spending_today(client):
    with user_scope(1):
        numbers = quick_numbers(1)
    assert numbers["expenses"] == [] and numbers["spent_today"] == 0
    assert bot.spent_text(numbers).endswith("Потрачено: 0,00 ₽\nСегодня трат ещё не было.")


def test_unknown_user_gets_no_numbers_and_no_database(client):
    assert not user_db_path(77).exists()
    with user_scope(77):
        assert quick_numbers(77) is None
    assert not user_db_path(77).exists()


def test_buttons_answer_with_the_numbers_of_the_sender(client, monkeypatch):
    monkeypatch.setattr(bot, "MINI_APP_URL", "https://budget.example")
    expense(client, 800, "Продукты")

    [(text, markup)] = press(bot.BTN_SPENT)
    assert text.startswith("🧾 Траты за 15.09") and "• Продукты — 800,00 ₽" in text and markup is None
    [(text, markup)] = press(bot.BTN_TODAY)
    assert text.startswith("📅 Сегодня, 15.09") and markup is None
    [(text, markup)] = press(bot.BTN_CARD)
    assert text.startswith("💳 На карте:") and markup is None

    # Somebody who never opened the app: no numbers, no new database file,
    # and a button to open the budget.
    [(text, markup)] = press(bot.BTN_TODAY, user_id=77)
    assert "Бюджет ещё не настроен" in text
    assert markup.inline_keyboard[0][0].web_app.url == "https://budget.example"
    assert not user_db_path(77).exists()

    [(text, markup)] = press(bot.BTN_OPEN)
    assert text == "Ваш личный бюджет:"
    assert markup.inline_keyboard[0][0].web_app.url == "https://budget.example"


def test_calculation_error_is_reported_not_raised(client, monkeypatch):
    def broken(user_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "quick_numbers", broken)
    [(text, markup)] = press(bot.BTN_TODAY)
    assert text == "Не получилось посчитать бюджет. Попробуйте чуть позже." and markup is None


@pytest.fixture
def irregular(tmp_path, monkeypatch):
    now = {"today": date(2026, 10, 1)}
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setattr(clock, "today", lambda: now["today"])
    monkeypatch.setattr("app.russian_calendar._remote_year", lambda _: None)
    init_db()
    ensure_user(1)
    with TestClient(app) as value:
        value.now = now
        yield value


def test_irregular_numbers_match_the_home_screen(irregular):
    client = irregular
    assert client.put("/api/budget-mode", json={
        "budget_mode": "irregular", "start_date": "2026-10-01", "start_total": 0, "start_reserve": 0,
    }).status_code == 200
    post(client, "/api/irregular/incomes", {"amount": 20000, "tx_date": "2026-10-01"})
    client.now["today"] = date(2026, 10, 3)
    expense(client, 700, "Продукты", day="2026-10-03")
    category = client.get("/api/bootstrap").json()["categories"][0]["id"]
    post(client, "/api/irregular/reserve/withdraw", {
        "amount": 500, "purpose": "pay_expense", "reason": "Стоматолог", "category_id": category})

    with user_scope(1):
        numbers = quick_numbers(1)
    flow = client.get("/api/irregular").json()
    assert numbers["mode"] == "irregular" and numbers["configured"]
    for key in ("today_target", "available_today", "spent_today", "overspend", "free_balance",
                "stretch_until", "days_left"):
        assert numbers[key] == flow[key], key
    # An expense paid from the reserve is not today's spending in this mode.
    assert numbers["spent_today"] == 700
    assert numbers["expenses"] == [{"title": "Продукты", "amount": 700.0}]
    assert [item["amount"] for item in numbers["reserve_paid"]] == [500.0]

    text = bot.spent_text(numbers)
    assert "Потрачено: 700,00 ₽\n• Продукты — 700,00 ₽" in text
    assert "Оплачено из резерва на непредвиденное:\n• " in text and "500,00 ₽" in text
    assert "Сверх плана" not in text
    text = bot.card_text(numbers)
    assert text.startswith(f"💳 Свободных денег: {bot.money(flow['free_balance'])} ₽")
    assert f"осталось {flow['days_left']} дн." in text


def test_irregular_mode_without_start(irregular):
    with connect() as con:
        con.execute("UPDATE settings SET budget_mode='irregular' WHERE user_id=1")
    with user_scope(1):
        numbers = quick_numbers(1)
    assert numbers == {"mode": "irregular", "configured": False}
    assert "Подработки» ещё не настроен" in bot.menu_reply_text(bot.BTN_TODAY, numbers)


def test_menu_keyboard_has_no_web_app_buttons():
    data = bot.MENU_KEYBOARD.to_dict()
    assert data["is_persistent"] is True and data["resize_keyboard"] is True
    assert [b["text"] for row in data["keyboard"] for b in row] == bot.MENU_BUTTONS
    # A web_app button here would open the app without initData (HTTP 401).
    assert all("web_app" not in b for row in data["keyboard"] for b in row)


def test_morning_report_brings_the_menu(monkeypatch):
    sent, recorded = [], []

    async def send_message(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(bot, "pending_morning_reports", lambda now: [{"user_id": 5}])
    monkeypatch.setattr(bot, "morning_report_text", lambda report: "Отчёт")
    monkeypatch.setattr(bot, "record_morning_report", recorded.append)
    application = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    asyncio.run(bot.send_morning_reports(application))
    assert sent == [{"chat_id": 5, "text": "Отчёт", "reply_markup": bot.MENU_KEYBOARD}]
    assert recorded == [{"user_id": 5}]


class FakeBot:
    def __init__(self, current):
        self.current = current
        self.set_calls = []

    async def get_chat_menu_button(self):
        if isinstance(self.current, Exception):
            raise self.current
        return self.current

    async def set_chat_menu_button(self, menu_button):
        self.set_calls.append(menu_button)


def test_menu_button_opens_the_app_and_keeps_a_configured_one(monkeypatch):
    monkeypatch.setattr(bot, "MINI_APP_URL", "https://budget.example")
    fresh = FakeBot(MenuButtonCommands())
    asyncio.run(bot.setup_menu_button(fresh))
    [button] = fresh.set_calls
    assert isinstance(button, MenuButtonWebApp) and button.web_app.url == "https://budget.example"

    configured = FakeBot(MenuButtonWebApp(text="Мой бюджет", web_app={"url": "https://other.example"}))
    asyncio.run(bot.setup_menu_button(configured))
    assert configured.set_calls == []

    # A Telegram error must not stop the bot from starting.
    failing = FakeBot(RuntimeError("network"))
    asyncio.run(bot.setup_menu_button(failing))
    assert failing.set_calls == []

    monkeypatch.setattr(bot, "MINI_APP_URL", "")
    unset = FakeBot(MenuButtonCommands())
    asyncio.run(bot.setup_menu_button(unset))
    assert unset.set_calls == []


def test_only_menu_buttons_in_private_chats_reach_the_menu_handler(monkeypatch):
    monkeypatch.setattr(bot, "BOT_TOKEN", "123456:TEST")
    application = bot.build_application()
    handlers = application.handlers[0]
    menu = next(h for h in handlers if getattr(h, "callback", None) is bot.menu_button)
    user = User(1, "Тест", False)

    def update(text, chat_type="private", edited=False):
        message = Message(1, datetime(2026, 9, 15), Chat(1, chat_type), from_user=user, text=text)
        return Update(1, edited_message=message) if edited else Update(1, message=message)

    for button in bot.MENU_BUTTONS:
        assert menu.check_update(update(button))
    assert not menu.check_update(update("Привет"))
    assert not menu.check_update(update("На сегодня"))
    assert not menu.check_update(update(bot.BTN_TODAY, chat_type="group"))
    assert not menu.check_update(update(bot.BTN_TODAY, edited=True))
    commands = {next(iter(h.commands)) for h in handlers if hasattr(h, "commands")}
    assert commands == {"start", "menu"}
