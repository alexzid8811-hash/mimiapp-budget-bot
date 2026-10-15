"""HTTP API for cards and accounts.  Balances come from :mod:`app.accounts`
through :mod:`app.accounts_engine`; this module validates requests and stores
accounts and their settings.  The budget is never changed here."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, model_validator

from . import accounts_engine as engine
from . import clock
from . import main as legacy
from .auth import TelegramUser, current_user
from .db import connect
from .validation import APIModel

router = APIRouter(prefix="/api", tags=["accounts"])

Kind = Literal["debit", "savings", "credit", "cash"]
Envelope = Literal["spending", "buffer", "piggy", "reserve"]


class AccountIn(APIModel):
    title: str = Field(min_length=1, max_length=40)
    emoji: str = Field(default="💳", min_length=1, max_length=8)
    kind: Kind = "debit"
    # The money on the account now; for a credit card the debt now (>= 0).
    balance: float = 0
    credit_limit: float | None = Field(default=None, ge=0)
    payment_day: int | None = Field(default=None, ge=1, le=31)

    @model_validator(mode="after")
    def check_credit(self) -> "AccountIn":
        if self.kind == "credit" and self.balance < 0:
            raise ValueError("Долг по кредитке не может быть отрицательным")
        return self


class AccountEditIn(APIModel):
    title: str = Field(min_length=1, max_length=40)
    emoji: str = Field(default="💳", min_length=1, max_length=8)
    credit_limit: float | None = Field(default=None, ge=0)
    payment_day: int | None = Field(default=None, ge=1, le=31)
    is_main: bool = False


class AccountOrderIn(APIModel):
    account_ids: list[int] = Field(min_length=1)


class AccountsSettingsIn(APIModel):
    enabled: bool


class SetupAccountIn(AccountIn):
    is_main: bool = False


class SetupIn(APIModel):
    accounts: list[SetupAccountIn] = Field(min_length=1, max_length=30)
    # Envelope -> index in ``accounts``.
    envelopes: dict[Envelope, int] = Field(default_factory=dict)


def _ready(user: TelegramUser) -> int:
    return legacy.user_ready(user)


def _account(con, uid: int, account_id: int) -> dict:
    row = con.execute(
        f"SELECT {engine.ACCOUNT_COLUMNS} FROM accounts WHERE id=? AND user_id=?", (account_id, uid)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Счёт не найден")
    return dict(row)


def _clean(title: str, emoji: str) -> tuple[str, str]:
    title, emoji = title.strip(), emoji.strip()
    if not title or not emoji:
        raise HTTPException(422, "Укажите название и эмодзи")
    return title, emoji


def _insert(con, uid: int, payload: AccountIn, order: int) -> int:
    title, emoji = _clean(payload.title, payload.emoji)
    credit = payload.kind == "credit"
    return con.execute(
        "INSERT INTO accounts(user_id,title,emoji,kind,opening_balance,opening_date,credit_limit,"
        "payment_day,sort_order) VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, title, emoji, payload.kind, payload.balance, clock.today().isoformat(),
         payload.credit_limit if credit else None, payload.payment_day if credit else None, order),
    ).lastrowid


def _next_order(con, uid: int) -> int:
    return con.execute(
        "SELECT COALESCE(MAX(sort_order),0)+1 FROM accounts WHERE user_id=? AND archived=0", (uid,)
    ).fetchone()[0]


def _active_ids(con, uid: int) -> list[int]:
    return [int(r[0]) for r in con.execute(
        "SELECT id FROM accounts WHERE user_id=? AND archived=0 ORDER BY sort_order,id", (uid,)
    )]


def _set_main(con, uid: int, account: dict) -> None:
    if account["kind"] == "credit":
        raise HTTPException(422, "Кредитка не может быть основным счётом")
    if account["archived"]:
        raise HTTPException(422, "Счёт в архиве: сначала верните его")
    con.execute("UPDATE settings SET main_account_id=? WHERE user_id=?", (account["id"], uid))


def _check_can_leave(con, uid: int, account: dict, action: str) -> None:
    """The main account and an account holding envelopes stay active until
    the main role and the envelopes are handed over to other accounts."""
    settings = engine.settings_for(con, uid)
    others = [i for i in _active_ids(con, uid) if i != account["id"]]
    if account["id"] == settings["main_account_id"] and others:
        raise HTTPException(422, f"Это основной счёт. Сначала сделайте основным другой счёт, потом {action}")
    labels = engine.ENVELOPE_LABELS[settings["mode"]]
    held = [labels.get(key, key) for key, account_id in engine.envelope_accounts(con, uid).items()
            if account_id == account["id"] and key in labels]
    if held and others:
        raise HTTPException(
            422, f"На счёте лежат конверты: {', '.join(held)}. Перенесите их на другие счета, потом {action}"
        )


def _check_empty(con, uid: int, account: dict) -> None:
    result = engine.calculate(engine.build_input(con, uid))
    state = result.states[int(account["id"])]
    if state.balance != 0:
        if account["kind"] == "credit":
            raise HTTPException(422, "По кредитке есть долг. Сначала погасите его или исправьте долг")
        raise HTTPException(
            422, "На счёте остались деньги. Сначала переведите остаток на другой счёт или исправьте остаток"
        )


def _forget(con, uid: int, account: dict) -> None:
    """The last active account leaves: nothing can be the main account, and
    the feature turns off (it needs at least one account)."""
    if _active_ids(con, uid):
        return
    con.execute(
        "UPDATE settings SET main_account_id=NULL,accounts_enabled=0,"
        "accounts_disabled_at=CASE WHEN accounts_enabled=1 THEN CURRENT_TIMESTAMP ELSE accounts_disabled_at END "
        "WHERE user_id=?",
        (uid,),
    )
    con.execute("DELETE FROM envelope_accounts WHERE user_id=?", (uid,))


def _archive(con, uid: int, account: dict) -> None:
    _check_can_leave(con, uid, account, "отправьте его в архив")
    _check_empty(con, uid, account)
    con.execute("UPDATE accounts SET archived=1 WHERE id=? AND user_id=?", (account["id"], uid))
    con.execute("DELETE FROM envelope_accounts WHERE user_id=? AND account_id=?", (uid, account["id"]))
    _forget(con, uid, account)


@router.get("/accounts")
def get_accounts(with_amounts: bool = False, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    return engine.snapshot(uid, with_amounts=with_amounts)


@router.post("/accounts")
def create_account(payload: AccountIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        account_id = _insert(con, uid, payload, _next_order(con, uid))
        settings = engine.settings_for(con, uid)
        if settings["main_account_id"] is None and payload.kind != "credit":
            con.execute("UPDATE settings SET main_account_id=? WHERE user_id=?", (account_id, uid))
        elif settings["main_account_id"] is None:
            raise HTTPException(422, "Сначала добавьте карту или счёт, куда приходит доход: кредитка не может быть основной")
    return engine.snapshot(uid)


@router.put("/accounts/order")
def reorder_accounts(payload: AccountOrderIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    ids = payload.account_ids
    if len(ids) != len(set(ids)):
        raise HTTPException(422, "Счета не должны повторяться")
    with connect() as con:
        if set(ids) != set(_active_ids(con, uid)):
            raise HTTPException(422, "Список счетов изменился. Обновите страницу")
        for position, account_id in enumerate(ids, start=1):
            con.execute("UPDATE accounts SET sort_order=? WHERE id=? AND user_id=?", (position, account_id, uid))
    return {"ok": True}


@router.put("/accounts/settings")
def save_accounts_settings(payload: AccountsSettingsIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        settings = engine.settings_for(con, uid)
        pending = 0
        if payload.enabled and not settings["enabled"]:
            main = settings["main_account_id"]
            if main is None or main not in _active_ids(con, uid):
                raise HTTPException(422, "Сначала добавьте карты и счета с суммами на них и выберите основной")
            if settings["disabled_at"]:
                pending = engine.operations_without_account(con, uid, settings["disabled_at"])
            con.execute(
                "UPDATE settings SET accounts_enabled=1,accounts_disabled_at=NULL WHERE user_id=?", (uid,)
            )
        elif not payload.enabled and settings["enabled"]:
            # Nothing is deleted: the section is hidden until it is switched on again.
            con.execute(
                "UPDATE settings SET accounts_enabled=0,accounts_disabled_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (uid,),
            )
    return {**engine.snapshot(uid), "operations_without_account": pending}


@router.post("/accounts/setup")
def setup_accounts(payload: SetupIn, user: TelegramUser = Depends(current_user)) -> dict:
    """First switch-on: the accounts with their balances, the main account
    and where every envelope lies, all at once."""
    uid = _ready(user)
    mains = [i for i, a in enumerate(payload.accounts) if a.is_main]
    if len(mains) != 1:
        raise HTTPException(422, "Выберите один основной счёт")
    if payload.accounts[mains[0]].kind == "credit":
        raise HTTPException(422, "Кредитка не может быть основным счётом")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        settings = engine.settings_for(con, uid)
        if settings["enabled"]:
            raise HTTPException(409, "Учёт по картам и счетам уже включён")
        if _active_ids(con, uid):
            raise HTTPException(409, "Карты и счета уже добавлены: включите учёт переключателем")
        keys = engine.ENVELOPES[settings["mode"]]
        for key, index in payload.envelopes.items():
            if key not in keys:
                raise HTTPException(422, "Этого конверта нет в текущем режиме")
            if not 0 <= index < len(payload.accounts):
                raise HTTPException(422, "Счёт конверта не найден")
            if payload.accounts[index].kind == "credit":
                raise HTTPException(422, "Конверт нельзя положить на кредитку")
        ids = [_insert(con, uid, account, position)
               for position, account in enumerate(payload.accounts, start=1)]
        today = clock.today().isoformat()
        for key in keys:
            account_id = ids[payload.envelopes.get(key, mains[0])]
            con.execute(
                "INSERT INTO envelope_accounts(user_id,envelope,account_id,assigned_on) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id,envelope) DO UPDATE SET account_id=excluded.account_id,"
                "assigned_on=excluded.assigned_on",
                (uid, key, account_id, today),
            )
        con.execute(
            "UPDATE settings SET main_account_id=?,accounts_enabled=1,accounts_disabled_at=NULL WHERE user_id=?",
            (ids[mains[0]], uid),
        )
    return engine.snapshot(uid)


@router.put("/accounts/{account_id}")
def update_account(account_id: int, payload: AccountEditIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    title, emoji = _clean(payload.title, payload.emoji)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        account = _account(con, uid, account_id)
        credit = account["kind"] == "credit"
        con.execute(
            "UPDATE accounts SET title=?,emoji=?,credit_limit=?,payment_day=? WHERE id=? AND user_id=?",
            (title, emoji, payload.credit_limit if credit else None, payload.payment_day if credit else None,
             account_id, uid),
        )
        if payload.is_main:
            _set_main(con, uid, account)
    return engine.snapshot(uid)


@router.post("/accounts/{account_id}/archive")
def archive_account(account_id: int, user: TelegramUser = Depends(current_user)) -> dict:
    uid = _ready(user)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        account = _account(con, uid, account_id)
        if account["archived"]:
            raise HTTPException(422, "Счёт уже в архиве")
        _archive(con, uid, account)
    return engine.snapshot(uid)


@router.post("/accounts/{account_id}/restore")
def restore_account(account_id: int, user: TelegramUser = Depends(current_user)) -> dict:
    """The account becomes active again at the end of the list.  Its balance
    and history stay; envelopes do not come back on their own."""
    uid = _ready(user)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        account = _account(con, uid, account_id)
        if not account["archived"]:
            raise HTTPException(422, "Счёт не в архиве")
        con.execute(
            "UPDATE accounts SET archived=0,sort_order=? WHERE id=? AND user_id=?",
            (_next_order(con, uid), account_id, uid),
        )
        if engine.settings_for(con, uid)["main_account_id"] is None and account["kind"] != "credit":
            con.execute("UPDATE settings SET main_account_id=? WHERE user_id=?", (account_id, uid))
    return engine.snapshot(uid)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, user: TelegramUser = Depends(current_user)) -> dict:
    """An account without a single operation is deleted for good; with
    operations it goes to the archive, so its history stays readable."""
    uid = _ready(user)
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        account = _account(con, uid, account_id)
        if engine.has_operations(con, uid, account_id):
            if account["archived"]:
                raise HTTPException(422, "По счёту были операции: он остаётся в архиве")
            _archive(con, uid, account)
            outcome = "archived"
        else:
            if not account["archived"]:
                _check_can_leave(con, uid, account, "удалите его")
            con.execute("DELETE FROM envelope_accounts WHERE user_id=? AND account_id=?", (uid, account_id))
            con.execute("DELETE FROM accounts WHERE id=? AND user_id=?", (account_id, uid))
            settings = engine.settings_for(con, uid)
            if settings["main_account_id"] == account_id:
                con.execute("UPDATE settings SET main_account_id=NULL WHERE user_id=?", (uid,))
            _forget(con, uid, account)
            outcome = "deleted"
    return {**engine.snapshot(uid), "outcome": outcome}


def check_account(con, uid: int, account_id: int | None, *, operation: str, current: int | None = None) -> dict | None:
    """Validate the account of an operation.  An archived account is accepted
    only when the operation already belongs to it (an edit of old history)."""
    if account_id is None:
        return None
    row = con.execute(
        f"SELECT {engine.ACCOUNT_COLUMNS} FROM accounts WHERE id=? AND user_id=?", (account_id, uid)
    ).fetchone()
    if row is None:
        raise HTTPException(422, "Счёт не найден")
    account = dict(row)
    if account["archived"] and account_id != current:
        raise HTTPException(422, "Счёт в архиве: выберите другой")
    if account["kind"] == "credit" and operation != "credit_expense":
        raise HTTPException(422, "Для этой операции кредитку выбрать нельзя")
    return account

