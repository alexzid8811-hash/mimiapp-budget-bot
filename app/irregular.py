"""Budget model for irregular income (side jobs): reserve, bills fund, free money.

All amounts are integer kopecks.  The module has no database access, so every
rule can be tested directly (like :mod:`app.ledger`).

Wallets
-------
* Emergency reserve: receives a percent of every income.  It never takes part
  in "можно сегодня"; money leaves it only by an explicit operation.
* Bills fund ("На обязательные"): money put aside for unpaid obligatory
  payments due within ``lookahead_days`` after an income.
* Free money: everything else; the daily limit is calculated from it.
* The piggy bank is kept outside and changes only by explicit operations.

Rules
-----
* Money that has not arrived yet is never counted: there is no forecast of
  income.  Only received money is split and spent.
* Every income is split in this order: reserve (``amount * percent / 100``,
  ROUND_HALF_UP to a kopeck, capped by the reserve target), the missing part of
  the bills due by ``income date + lookahead_days`` (by due date), free money.
* Stretch date D: an income that adds free money sets
  ``D = income date + stretch_days - 1``.  When D has passed without such an
  income, the window is extended: ``D = today + stretch_days - 1``.
* Limit for a day = (free money at the start of the day + money added to free
  money that day) // days through D.  An overspend therefore reduces the
  remaining days, an underspend carries over.
* The calculation is a replay of every operation since the start date, day by
  day.  Within one day: incomes (by id), reserve deposits, bill payments,
  everyday spending and transfers, reserve withdrawals.  Backdated income, an
  edit or a deletion therefore recalculates everything after it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal


ONE_DAY = timedelta(days=1)
DESTINATIONS = ("split", "reserve", "piggy")


@dataclass(frozen=True)
class IrregularSettings:
    reserve_percent: float = 10
    reserve_target: int | None = None   # kopecks, None = always put aside
    stretch_days: int = 14
    lookahead_days: int = 30


@dataclass(frozen=True)
class Income:
    id: int
    day: date
    amount: int
    percent: float = 0
    destination: str = "split"   # split | reserve | piggy
    source: str = ""


@dataclass(frozen=True)
class Bill:
    """One occurrence of an obligatory payment."""
    rule_id: int
    due: date
    amount: int                  # planned amount
    title: str = ""
    paid_on: date | None = None
    actual: int = 0              # paid amount
    to_piggy: int = 0            # remainder explicitly moved to the piggy bank
    payment_id: int | None = None

    @property
    def key(self) -> tuple[int, date]:
        return (self.rule_id, self.due)


@dataclass(frozen=True)
class ReserveMove:
    id: int
    day: date
    direction: str               # deposit | withdraw
    amount: int
    kind: str                    # deposit | from_free | pay_expense | to_free | cover_overspend
    reason: str = ""
    expense_id: int | None = None


@dataclass(frozen=True)
class Split:
    reserve: int = 0
    bills: int = 0
    free: int = 0
    piggy: int = 0

    @property
    def total(self) -> int:
        return self.reserve + self.bills + self.free + self.piggy


@dataclass
class IrregularInput:
    start: date
    today: date
    start_total: int = 0           # all money at the start, without the piggy bank
    start_reserve: int = 0         # part of it that is already the reserve
    settings: IrregularSettings = field(default_factory=IrregularSettings)
    incomes: list[Income] = field(default_factory=list)
    bills: list[Bill] = field(default_factory=list)
    reserve_moves: list[ReserveMove] = field(default_factory=list)
    spent: dict[date, int] = field(default_factory=dict)       # everyday expenses
    to_piggy: dict[date, int] = field(default_factory=dict)    # day remainder -> piggy
    free_in: dict[date, int] = field(default_factory=dict)     # piggy -> free, spread
    free_cover: dict[date, int] = field(default_factory=dict)  # piggy -> today only


@dataclass(frozen=True)
class ReserveEntry:
    day: date
    kind: str                    # start | income | income_reserve | deposit | from_free | ...
    amount: int                  # signed
    balance: int
    reason: str = ""
    ref_id: int | None = None


@dataclass(frozen=True)
class Shortfall:
    amount: int
    due: date


@dataclass
class IrregularResult:
    today: date
    stretch_until: date
    days_left: int
    window_extended: bool
    free_start_today: int
    added_today: int
    cover_today: int
    spent_today: int
    out_today: int
    today_limit: int
    available_today: int
    overspend: int
    free_end_today: int
    tomorrow_limit: int
    reserve: int
    bills_fund: dict[tuple[int, date], int]
    splits: dict[int, Split]
    reserve_history: list[ReserveEntry]
    reserve_violation: date | None
    last_income: date | None
    days_without_income: int
    shortfall: Shortfall | None
    bills: list[Bill]

    @property
    def bills_total(self) -> int:
        return sum(self.bills_fund.values())


def reserve_share(amount: int, percent: float) -> int:
    """``amount * percent / 100`` rounded half up to a kopeck."""
    value = Decimal(amount) * Decimal(str(percent)) / Decimal(100)
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def split_income(
    amount: int, percent: float, *, destination: str = "split", reserve_balance: int = 0,
    reserve_target: int | None = None, bills_need: int = 0,
) -> Split:
    """Split one income: reserve, then the bills fund, then free money."""
    if destination == "piggy":
        return Split(piggy=amount)
    if destination == "reserve":
        return Split(reserve=amount)
    share = reserve_share(amount, percent)
    if reserve_target is not None:
        share = min(share, max(0, reserve_target - reserve_balance))
    rest = amount - share
    bills = min(rest, max(0, bills_need))
    return Split(reserve=share, bills=bills, free=rest - bills)


def stretch_end(day: date, stretch_days: int) -> date:
    return day + timedelta(days=stretch_days - 1)


def replay(inp: IrregularInput) -> IrregularResult:
    if inp.today < inp.start:
        raise ValueError("Дата старта позже сегодняшнего дня")
    cfg = inp.settings
    n = max(1, int(cfg.stretch_days))
    k = max(0, int(cfg.lookahead_days))

    incomes: dict[date, list[Income]] = {}
    for income in sorted(inp.incomes, key=lambda i: (i.day, i.id)):
        if inp.start <= income.day <= inp.today:
            incomes.setdefault(income.day, []).append(income)
    deposits: dict[date, list[ReserveMove]] = {}
    withdrawals: dict[date, list[ReserveMove]] = {}
    for move in sorted(inp.reserve_moves, key=lambda m: (m.day, m.id)):
        if inp.start <= move.day <= inp.today:
            target = deposits if move.direction == "deposit" else withdrawals
            target.setdefault(move.day, []).append(move)
    payments: dict[date, list[Bill]] = {}
    for bill in inp.bills:
        if bill.paid_on is not None and inp.start <= bill.paid_on <= inp.today:
            payments.setdefault(bill.paid_on, []).append(bill)
    # Occurrences that money can be put aside for.
    needs = sorted((b for b in inp.bills if b.due >= inp.start), key=lambda b: (b.due, b.rule_id))
    paid: set[tuple[int, date]] = set()
    fund: dict[tuple[int, date], int] = {}

    def bills_need(day: date) -> int:
        horizon = day + timedelta(days=k)
        return sum(
            max(0, b.amount - fund.get(b.key, 0))
            for b in needs if b.due <= horizon and b.key not in paid
        )

    def fill_bills(day: date, money: int) -> int:
        horizon = day + timedelta(days=k)
        used = 0
        for b in needs:
            if b.due > horizon or money - used <= 0:
                break
            if b.key in paid:
                continue
            missing = max(0, b.amount - fund.get(b.key, 0))
            take = min(missing, money - used)
            if take:
                fund[b.key] = fund.get(b.key, 0) + take
                used += take
        return used

    reserve = inp.start_reserve
    history: list[ReserveEntry] = []
    violation: date | None = None
    if inp.start_reserve:
        history.append(ReserveEntry(inp.start, "start", inp.start_reserve, reserve, "Резерв на старте"))
    rest = inp.start_total - inp.start_reserve
    free = rest - fill_bills(inp.start, max(0, rest))
    stretch = stretch_end(inp.start, n)
    extended = False
    splits: dict[int, Split] = {}
    last_income: date | None = None
    today_numbers = (0, 0, 0, 0, 0)

    def reserve_move(day: date, kind: str, signed: int, reason: str, ref: int | None) -> None:
        nonlocal reserve, violation
        reserve += signed
        history.append(ReserveEntry(day, kind, signed, reserve, reason, ref))
        if reserve < 0 and violation is None:
            violation = day

    day = inp.start
    while day <= inp.today:
        if day > stretch:
            stretch, extended = stretch_end(day, n), True
        free_start = free
        added = cover = spent = out = 0

        for income in incomes.get(day, []):
            split = split_income(
                income.amount, income.percent, destination=income.destination,
                reserve_balance=reserve, reserve_target=cfg.reserve_target,
                bills_need=bills_need(day),
            )
            if split.bills:
                fill_bills(day, split.bills)
            splits[income.id] = split
            if split.reserve:
                kind = "income_reserve" if income.destination == "reserve" else "income"
                reserve_move(day, kind, split.reserve, income.source, income.id)
            free += split.free
            added += split.free
            if split.free > 0:
                stretch, extended = stretch_end(day, n), False
            last_income = day

        for move in deposits.get(day, []):
            reserve_move(day, move.kind, move.amount, move.reason, move.id)
            if move.kind == "from_free":
                free -= move.amount
                out += move.amount

        for bill in sorted(payments.get(day, []), key=lambda b: (b.payment_id or 0, b.rule_id)):
            put_aside = fund.pop(bill.key, 0)
            paid.add(bill.key)
            # Released money pays the bill; a remainder returns to free money
            # (or to the piggy bank), an overpayment is paid from free money.
            difference = put_aside - bill.actual - bill.to_piggy
            free += difference
            if difference > 0:
                added += difference
            else:
                spent -= difference

        value = inp.spent.get(day, 0)
        free -= value
        spent += value
        value = inp.to_piggy.get(day, 0)
        free -= value
        out += value
        value = inp.free_in.get(day, 0)
        free += value
        added += value
        value = inp.free_cover.get(day, 0)
        free += value
        cover += value

        for move in withdrawals.get(day, []):
            reserve_move(day, move.kind, -move.amount, move.reason, move.id)
            if move.kind == "to_free":
                free += move.amount
                added += move.amount
            elif move.kind == "cover_overspend":
                free += move.amount
                cover += move.amount

        if day == inp.today:
            today_numbers = (free_start, added, cover, spent, out)
        day += ONE_DAY

    free_start, added, cover, spent, out = today_numbers
    today = inp.today
    days_left = (stretch - today).days + 1
    today_limit = max(0, (free_start + added) // days_left)
    available = today_limit + cover - spent - out
    tomorrow = today + ONE_DAY
    tomorrow_end = stretch if stretch >= tomorrow else stretch_end(tomorrow, n)
    tomorrow_limit = max(0, free // ((tomorrow_end - tomorrow).days + 1))

    horizon = today + timedelta(days=k)
    missing = [
        (b.due, b.amount - fund.get(b.key, 0)) for b in needs
        if b.key not in paid and b.due <= horizon and fund.get(b.key, 0) < b.amount
    ]
    shortfall = Shortfall(sum(m for _, m in missing), min(d for d, _ in missing)) if missing else None
    anchor = last_income or inp.start

    return IrregularResult(
        today=today,
        stretch_until=stretch,
        days_left=days_left,
        window_extended=extended,
        free_start_today=free_start,
        added_today=added,
        cover_today=cover,
        spent_today=spent,
        out_today=out,
        today_limit=today_limit,
        available_today=available,
        overspend=max(0, -available),
        free_end_today=free,
        tomorrow_limit=tomorrow_limit,
        reserve=reserve,
        bills_fund={key: value for key, value in fund.items() if value},
        splits=splits,
        reserve_history=history,
        reserve_violation=violation,
        last_income=last_income,
        days_without_income=(today - anchor).days,
        shortfall=shortfall,
        bills=sorted(inp.bills, key=lambda b: (b.due, b.rule_id)),
    )
