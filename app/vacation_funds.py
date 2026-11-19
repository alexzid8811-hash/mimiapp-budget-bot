"""Allocate vacation money already in the piggy bank to the affected paydays."""
from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction

from . import planning
from .budget import add_months
from .money import amount, cents
from .payroll import vacation_workdays_in_month


LINKED_INCOME_MESSAGE = "Это доход, связанный с отпускными. Сначала снимите отметку в записи отпуска"


def _saved(con, uid):
    vacations = [dict(row) for row in con.execute(
        "SELECT * FROM vacations WHERE user_id=? AND reserve_in_piggy=1 ORDER BY start_date,id",
        (uid,),
    )]
    transfers = defaultdict(dict)
    for row in con.execute(
        "SELECT vacation_id,vacation_part,amount,movement_date FROM piggy_bank_movements "
        "WHERE user_id=? AND purpose='vacation_buffer'",
        (uid,),
    ):
        transfers[row["vacation_id"]][row["vacation_part"]] = (cents(row["amount"]), row["movement_date"])
    return vacations, transfers


def outstanding_cents(con, uid):
    vacations, transfers = _saved(con, uid)
    return sum(
        max(0, cents(v["amount"]) - sum(value for value, _ in transfers[v["id"]].values()))
        for v in vacations
    )


def free_before_reserves(con, uid):
    balance = con.execute(
        "SELECT COALESCE(SUM(CASE WHEN direction='deposit' THEN amount ELSE -amount END),0) "
        "FROM piggy_bank_movements WHERE user_id=?", (uid,),
    ).fetchone()[0]
    goals = con.execute(
        "SELECT COALESCE(SUM(amount),0) FROM piggy_goal_moves WHERE user_id=?", (uid,),
    ).fetchone()[0]
    return cents(round(balance, 2)) - cents(round(goals, 2))


def protected_cents(con, uid):
    outstanding = outstanding_cents(con, uid)
    return min(outstanding, max(0, free_before_reserves(con, uid))) if outstanding else 0


def save_deposit(con, uid, vacation_id, value, received, today):
    """Book one receipt, or link a matching receipt entered before this feature."""
    from . import accounts_engine

    if received > today or cents(value) <= 0:
        raise ValueError("Укажите полученную сумму и фактическую дату получения отпускных")
    day = received.isoformat()
    existing = con.execute(
        "SELECT id,amount,movement_date,purpose FROM piggy_bank_movements "
        "WHERE user_id=? AND vacation_id=? AND vacation_part='deposit'", (uid, vacation_id),
    ).fetchone()
    if existing:
        if cents(value) == cents(existing["amount"]) and day == existing["movement_date"]:
            return
        if existing["purpose"] == "vacation_existing_deposit":
            raise ValueError("Эти отпускные уже связаны с отдельным пополнением. Сначала снимите отметку в отпуске")
        con.execute("UPDATE piggy_bank_movements SET amount=?,movement_date=? WHERE id=?",
                    (value, day, existing["id"]))
        con.execute("UPDATE account_incomes SET amount=?,date=? WHERE user_id=? AND piggy_movement_id=?",
                    (value, day, uid, existing["id"]))
        return
    if con.execute(
        "SELECT 1 FROM piggy_bank_movements WHERE user_id=? AND vacation_id=? AND purpose='vacation_buffer'",
        (uid, vacation_id),
    ).fetchone():
        return  # A legacy reserve has already been released using the funds on record.
    matches = [row for row in con.execute(
        "SELECT m.id,m.amount,EXISTS(SELECT 1 FROM piggy_goal_moves g WHERE g.piggy_movement_id=m.id) AS on_goal "
        "FROM piggy_bank_movements m WHERE m.user_id=? AND m.direction='deposit' AND m.source='external' "
        "AND m.movement_date=? AND m.purpose IS NULL AND m.vacation_id IS NULL", (uid, day),
    ) if cents(row["amount"]) == cents(value)]
    if len(matches) > 1:
        raise ValueError("В копилке несколько пополнений с этой датой и суммой. Проверьте историю копилки")
    if matches:
        if matches[0]["on_goal"]:
            raise ValueError("Эта сумма уже отложена на цель. Перенесите её в «Без цели», чтобы зарезервировать отпускные")
        con.execute(
            "UPDATE piggy_bank_movements SET vacation_id=?,vacation_part='deposit',purpose='vacation_existing_deposit' "
            "WHERE id=?", (vacation_id, matches[0]["id"]),
        )
        return
    movement_id = con.execute(
        "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date,note,source,purpose,"
        "vacation_id,vacation_part) VALUES(?,'deposit',?,?,'Отпускные','external','vacation_deposit',?,'deposit')",
        (uid, value, day, vacation_id),
    ).lastrowid
    accounts_engine.link_piggy(con, uid, movement_id, direction="deposit", source="external", value=value, day=day)


def remove_deposit(con, uid, vacation_id):
    con.execute(
        "UPDATE piggy_bank_movements SET vacation_id=NULL,vacation_part=NULL,purpose=NULL "
        "WHERE user_id=? AND vacation_id=? AND purpose='vacation_existing_deposit'", (uid, vacation_id),
    )
    con.execute(
        "DELETE FROM piggy_bank_movements WHERE user_id=? AND vacation_id=? AND purpose='vacation_deposit'",
        (uid, vacation_id),
    )


def linked_income(con, uid, tx_id):
    return con.execute(
        "SELECT 1 FROM piggy_bank_movements WHERE user_id=? AND income_transaction_id=? AND vacation_part='deposit'",
        (uid, tx_id),
    ).fetchone() is not None


def _payout_parts(vacations, events):
    parts = defaultdict(dict)
    for event in events:
        if event.get("kind") not in ("advance", "salary"):
            continue
        year, month, kind = event["accrual_year"], event["accrual_month"], event["kind"]
        key = f"{year:04d}-{month:02d}:{kind}"
        calc = event["calculation"]
        for vacation in vacations:
            workdays = vacation_workdays_in_month(year, month, [vacation])
            days = sum((day.day <= 15) == (kind == "advance") for day in workdays)
            if days:
                parts[vacation["id"]][key] = (event["date"], days, Fraction(
                    days * cents(calc["salary_net"]), calc["workdays_total"],
                ))
    return parts


def _split(total, weights):
    """Allocate whole kopecks, giving rounding remainders to the largest fractions."""
    weight_sum = sum(weights)
    exact = [total * weight / weight_sum for weight in weights]
    allocated = [int(value) for value in exact]
    ranked = sorted(range(len(exact)), key=lambda i: exact[i] - allocated[i], reverse=True)
    for index in ranked[:total - sum(allocated)]:
        allocated[index] += 1
    return allocated


def rows(con, uid, today):
    vacations, transfers = _saved(con, uid)
    if not vacations:
        return []
    start = min(date.fromisoformat(v["start_date"]) for v in vacations)
    end = max(date.fromisoformat(v["end_date"]) for v in vacations)
    events = planning.income_events(uid, start.replace(day=1), add_months(end.replace(day=1), 2))
    parts = _payout_parts(vacations, events)
    paydays = sorted({event["date"] for event in events if event["is_payday"]})
    result = []
    for vacation in vacations:
        vid = vacation["id"]
        settled = transfers[vid]
        remaining = max(0, cents(vacation["amount"]) - sum(value for value, _ in settled.values()))
        pending = [(key, part) for key, part in parts[vid].items() if key not in settled]
        weights = [part[2] for _, part in pending]
        values = _split(remaining, weights if any(weights) else [Fraction(part[1]) for _, part in pending]) if pending else []
        planned = dict(zip((key for key, _ in pending), values))
        for key, (payday, days, _) in parts[vid].items():
            transferred, transfer_date = settled.get(key, (0, None))
            value = transferred or planned[key]
            if value == 0:
                continue
            end = next((day for day in paydays if day > payday), None)
            result.append({
                "vacation_id": vid, "part": key, "payday": payday.isoformat(), "workdays": days,
                "start_date": vacation["start_date"], "end_date": vacation["end_date"],
                "period_end": (end - timedelta(days=1)).isoformat() if end else None,
                "amount_cents": value, "transfer_date": transfer_date,
            })
        if remaining and not pending:
            result.append({
                "vacation_id": vid, "part": None, "payday": None, "workdays": 0,
                "start_date": vacation["start_date"], "end_date": vacation["end_date"],
                "period_end": None, "amount_cents": remaining, "transfer_date": None,
            })
    free = max(0, free_before_reserves(con, uid))
    result.sort(key=lambda row: (row["payday"] or "9999", row["vacation_id"], row["part"] or ""))
    for row in result:
        value = row.pop("amount_cents")
        covered = 0 if row["transfer_date"] else min(free, value)
        free -= covered
        row["amount"] = amount(value)
        row["reserved"] = amount(covered)
        row["can_transfer"] = bool(
            covered == value and row["payday"] and row["payday"] <= today.isoformat()
        )
    return result
