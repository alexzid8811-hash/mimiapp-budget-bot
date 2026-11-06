"""Цели копилки: «Отпуск — 50 000 ₽» и сколько на неё уже накоплено.

A goal is a label over the piggy bank, not a pot of its own: the money set
aside for a goal stays in the piggy bank.  The budget never sees goals — the
piggy-bank balance and every calculation of the day stay exactly as they are.

* ``saved`` of a goal is the sum of its moves: + set aside, − taken;
* ``unassigned`` is the piggy-bank money set aside for no goal:
  balance − the sum of ``saved``.

A deposit or a withdrawal on the piggy-bank screen may name a goal: its move is
linked to the piggy-bank movement and disappears together with it.  Money
already in the piggy bank moves with «Перенести»: from no goal to a goal, back,
or from one goal to another.  It never changes the piggy-bank balance.

Explicit piggy-bank operations keep the goals within the balance: a withdrawal
for no goal takes only unassigned money.  Operations made elsewhere (an
expense paid from the piggy bank, a changed budget) can still leave the goals
above the balance; the screen then shows the shortfall.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, field_validator

from . import clock
from . import main as legacy
from .auth import TelegramUser, current_user
from .money import amount, cents
from .validation import APIModel


router = APIRouter(prefix="/api/piggy-goals", tags=["piggy-goals"])


class PiggyGoalIn(APIModel):
    title: str = Field(min_length=1, max_length=40)
    target: float = Field(gt=0, le=1_000_000_000)

    @field_validator("title")
    @classmethod
    def stripped(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Введите название цели")
        return value


class PiggyGoalTransferIn(APIModel):
    amount: float = Field(gt=0)
    # None: the piggy-bank money set aside for no goal.
    from_goal_id: int | None = None
    to_goal_id: int | None = None


def _money(value: int) -> str:
    whole, part = divmod(abs(value), 100)
    text = f"{whole:,}".replace(",", " ") + (f",{part:02d}" if part else "")
    return f"{'-' if value < 0 else ''}{text} ₽"


def balance_cents(con: sqlite3.Connection, uid: int) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(CASE WHEN direction='deposit' THEN amount ELSE -amount END),0) "
        "FROM piggy_bank_movements WHERE user_id=?",
        (uid,),
    ).fetchone()
    return cents(round(float(row[0]), 2))


def _goals(con: sqlite3.Connection, uid: int) -> list[dict]:
    return [dict(row) for row in con.execute(
        "SELECT g.id,g.title,g.target,COALESCE(SUM(m.amount),0) AS saved FROM piggy_goals g "
        "LEFT JOIN piggy_goal_moves m ON m.goal_id=g.id AND m.user_id=g.user_id "
        "WHERE g.user_id=? GROUP BY g.id ORDER BY g.id",
        (uid,),
    )]


def saved_cents(con: sqlite3.Connection, uid: int, goal_id: int | None = None) -> int:
    """Set aside for one goal, or for all of them."""
    sql = "SELECT COALESCE(SUM(amount),0) FROM piggy_goal_moves WHERE user_id=?"
    params: tuple = (uid,)
    if goal_id is not None:
        sql += " AND goal_id=?"
        params = (uid, goal_id)
    return cents(round(float(con.execute(sql, params).fetchone()[0]), 2))


def unassigned_cents(con: sqlite3.Connection, uid: int) -> int:
    """Piggy-bank money set aside for no goal (negative: goals above the balance)."""
    return balance_cents(con, uid) - saved_cents(con, uid)


def summary(con: sqlite3.Connection, uid: int) -> dict:
    """The goals with their progress, for the piggy-bank screen."""
    goals = []
    for row in _goals(con, uid):
        target, saved = cents(row["target"]), cents(round(float(row["saved"]), 2))
        goals.append({
            "id": int(row["id"]),
            "title": row["title"],
            "target": amount(target),
            "saved": amount(saved),
            "left": amount(max(0, target - saved)),
            # Whole percent, rounded down: 99.6 % is not yet 100 %.
            "percent": saved * 100 // target if target else 0,
            "reached": saved >= target,
        })
    free = unassigned_cents(con, uid)
    return {
        "goals": goals,
        "goals_saved": amount(sum(cents(g["saved"]) for g in goals)),
        "unassigned": amount(max(0, free)),
        "goals_shortfall": amount(max(0, -free)),
    }


def goal_title(con: sqlite3.Connection, uid: int, goal_id: int) -> str:
    row = con.execute("SELECT title FROM piggy_goals WHERE id=? AND user_id=?", (goal_id, uid)).fetchone()
    if row is None:
        raise HTTPException(404, "Цель копилки не найдена")
    return row["title"]


def check_withdrawal(con: sqlite3.Connection, uid: int, value: float, goal_id: int | None) -> None:
    """Before a piggy-bank withdrawal: a goal gives at most what is set aside
    for it; without a goal only the unassigned money can leave."""
    if goal_id is not None:
        title = goal_title(con, uid, goal_id)
        saved = saved_cents(con, uid, goal_id)
        if cents(value) > saved:
            raise HTTPException(422, f"На цель «{title}» отложено {_money(saved)}")
        return
    if saved_cents(con, uid) <= 0:
        return  # no money set aside: the piggy-bank history check is enough
    free = max(0, unassigned_cents(con, uid))
    if cents(value) > free:
        raise HTTPException(
            422,
            f"Без цели в копилке {_money(free)}, остальное отложено на цели. "
            "Выберите цель, с которой взять деньги",
        )


def link_movement(con: sqlite3.Connection, uid: int, movement_id: int, goal_id: int | None,
                  direction: str, value: float, day: str) -> None:
    """A deposit to a goal or a withdrawal from it, tied to its piggy-bank movement."""
    if goal_id is None:
        return
    goal_title(con, uid, goal_id)
    signed = value if direction == "deposit" else -value
    con.execute(
        "INSERT INTO piggy_goal_moves(user_id,goal_id,amount,move_date,piggy_movement_id) VALUES(?,?,?,?,?)",
        (uid, goal_id, signed, day, movement_id),
    )


def check_fits(con: sqlite3.Connection, uid: int) -> None:
    """After an explicit piggy-bank change: the goals stay within the balance."""
    if unassigned_cents(con, uid) < 0 and saved_cents(con, uid) > 0:
        raise HTTPException(
            422, "Тогда на цели будет отложено больше, чем лежит в копилке. Сначала уберите деньги с цели",
        )


def _snapshot(uid: int) -> dict:
    from .cashflow_app import piggy_bank_snapshot

    return piggy_bank_snapshot(uid)


@router.get("")
def get_goals(user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        return summary(con, uid)


@router.post("")
def create_goal(payload: PiggyGoalIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        if con.execute("SELECT COUNT(*) FROM piggy_goals WHERE user_id=?", (uid,)).fetchone()[0] >= 30:
            raise HTTPException(422, "Целей уже 30 — удалите ненужные")
        con.execute("INSERT INTO piggy_goals(user_id,title,target) VALUES(?,?,?)", (uid, payload.title, payload.target))
    return _snapshot(uid)


@router.put("/{goal_id}")
def update_goal(goal_id: int, payload: PiggyGoalIn, user: TelegramUser = Depends(current_user)) -> dict:
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        goal_title(con, uid, goal_id)
        con.execute(
            "UPDATE piggy_goals SET title=?,target=? WHERE id=? AND user_id=?",
            (payload.title, payload.target, goal_id, uid),
        )
    return _snapshot(uid)


@router.delete("/{goal_id}")
def delete_goal(goal_id: int, user: TelegramUser = Depends(current_user)) -> dict:
    """The goal goes; its money stays in the piggy bank, set aside for no goal."""
    uid = legacy.user_ready(user)
    with legacy.connect() as con:
        goal_title(con, uid, goal_id)
        con.execute("DELETE FROM piggy_goals WHERE id=? AND user_id=?", (goal_id, uid))
    return _snapshot(uid)


@router.post("/transfer")
def transfer_goal_money(payload: PiggyGoalTransferIn, user: TelegramUser = Depends(current_user)) -> dict:
    """«Перенести» money already in the piggy bank: from no goal to a goal
    («Отложить»), back («Убрать»), or between two goals.  The piggy-bank
    balance does not change."""
    uid = legacy.user_ready(user)
    if payload.from_goal_id == payload.to_goal_id:
        raise HTTPException(422, "Выберите, откуда и куда перенести деньги")
    value = cents(payload.amount)
    day = clock.today().isoformat()
    with legacy.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        if payload.to_goal_id is not None:
            goal_title(con, uid, payload.to_goal_id)
        if payload.from_goal_id is None:
            free = max(0, unassigned_cents(con, uid))
            if value > free:
                raise HTTPException(422, f"Без цели в копилке {_money(free)}")
        else:
            title = goal_title(con, uid, payload.from_goal_id)
            saved = saved_cents(con, uid, payload.from_goal_id)
            if value > saved:
                raise HTTPException(422, f"На цель «{title}» отложено {_money(saved)}")
        for goal_id, signed in ((payload.from_goal_id, -value), (payload.to_goal_id, value)):
            if goal_id is not None:
                con.execute(
                    "INSERT INTO piggy_goal_moves(user_id,goal_id,amount,move_date) VALUES(?,?,?,?)",
                    (uid, goal_id, amount(signed), day),
                )
    return _snapshot(uid)
