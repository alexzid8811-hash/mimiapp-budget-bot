"""Budget model: card, buffer and piggy bank.

All amounts are integer kopecks.  The module has no database access, so every
rule of the model can be tested directly.

Calendar
--------
* A payday is the actual date of a salary or advance, already moved to the
  last working day before a non-working nominal date.
* Buffer period: ``[payday, next payday - 1]``.  The payment arrives in the
  buffer on the payday itself, and obligatory payments are held on their exact
  due dates, including a bill that falls on the payday.
* Card period: ``[payday + 1, next payday]``.  The payday still belongs to the
  previous card period, so the old money can be spent on the 7th/22nd.  The
  money for the next card period is taken from the buffer on the payday and
  becomes spendable on the following day.

Rules
-----
* The buffer never participates in "можно сегодня": it is never divided into
  days and never pays for an overspend.  Its only outflows are obligatory
  payments and the funding of card periods on paydays.
* A card period receives ``daily_rate * days``.  The rate is the largest equal
  daily amount such that the buffer stays non-negative on every day of the
  forecast horizon (all obligations are covered on their dates, and money from
  strong periods is kept for future cash gaps).
* A yearly obligatory payment is saved up in equal parts: on every funding
  day in the 12 months before its due date (from the day the payment was
  planned) the buffer keeps one part, so the card periods share the payment
  evenly instead of the few periods just before the due date.
* The card balance is carried over.  An overspend reduces the remaining days
  of the current card period; on the last day it reduces the next period, whose
  new money is added to the already reduced card balance.
* The piggy bank changes only by explicit operations.  A piggy -> card
  transfer either increases the period (spread over the remaining days) or
  covers today's overspend directly.
* An optional daily maximum («Максимум в день») caps the rate: a card period
  receives at most ``cap * days`` and the rest stays in the buffer.  On the
  first day of a period, and on the day the cap is changed, the card balance
  (including money left from the previous period) is trimmed to
  ``cap * remaining days``; the excess returns to the buffer.  A raised cap
  tops the current period up from the buffer by the stored ``cap_topups``.
  With «избыток в копилку» the excess goes to the piggy bank instead: the
  buffer pays the period as without the maximum (so it never sags) and the
  part above the maximum, like the trimmed card money, goes to the piggy bank.
* An optional minimum of the buffer («Минимум буфера») is never part of a
  funding decision: the rate keeps the buffer at or above it on every day,
  on top of the money saved up for yearly payments.  Obligatory payments are
  still paid from the buffer, so they may take it below the minimum.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Iterable

from .budget import add_months


ONE_DAY = timedelta(days=1)


def daterange(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += ONE_DAY


@dataclass(frozen=True)
class CardPeriod:
    start: date      # first day the money can be spent
    end: date        # last day, always an actual payday
    funded_on: date  # day the money leaves the buffer

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def card_periods(first_day: date, paydays: list[date], until: date) -> list[CardPeriod]:
    """Card periods from ``first_day`` through the last one starting by ``until``.

    The first period may be shortened (the start date of the calculation) and
    is funded on that start date.  Every later period starts the day after a
    payday and is funded on that payday.
    """
    later = sorted(p for p in set(paydays) if p >= first_day)
    if not later:
        raise ValueError("Нет ни одной даты выплаты после даты старта")
    result = [CardPeriod(first_day, later[0], first_day)]
    for payday, next_payday in zip(later, later[1:]):
        if payday + ONE_DAY > until:
            break
        result.append(CardPeriod(payday + ONE_DAY, next_payday, payday))
    return result


@dataclass(frozen=True)
class RateDecision:
    daily: int
    shortfall: int
    shortfall_date: date | None


def safe_daily_rate(
    *,
    opening: int,
    start: date,
    end: date,
    flows: dict[date, int],
    funded_days: dict[date, int],
) -> RateDecision:
    """Largest equal daily rate that keeps the buffer non-negative.

    ``opening`` is the buffer before the flows of ``start``.  ``flows`` are
    buffer income minus obligatory payments by date.  ``funded_days`` is the
    number of card days funded on each funding date (paid in advance for the
    whole card period).  A card period near the horizon is counted in full,
    which is conservative.
    """
    balance = opening
    funded = 0
    best: int | None = None
    lowest = 0
    lowest_date: date | None = None
    for day in daterange(start, end):
        balance += flows.get(day, 0)
        funded += funded_days.get(day, 0)
        if balance < lowest:
            lowest, lowest_date = balance, day
        if funded > 0:
            candidate = balance // funded
            best = candidate if best is None else min(best, candidate)
    return RateDecision(max(0, best or 0), -lowest, lowest_date)


def spend_evenly(balance: int, days: int) -> tuple[int, int]:
    """Spend ``balance / remaining days`` each day; return (first limit, leftover)."""
    first: int | None = None
    for remaining in range(days, 0, -1):
        limit = max(0, balance // remaining)
        if first is None:
            first = limit
        balance -= limit
    return first or 0, balance


def cap_rule(caps: list[tuple], day: date) -> tuple[int | None, bool]:
    """The daily maximum in effect on ``day`` (None: no maximum) and whether
    its excess goes to the piggy bank.  Entries: (since, cap[, to_piggy])."""
    value: tuple[int | None, bool] = (None, False)
    for entry in caps:
        if entry[0] > day:
            break
        value = (entry[1], bool(entry[2]) if len(entry) > 2 else False)
    return value


def cap_on(caps: list[tuple], day: date) -> int | None:
    """The daily maximum in effect on ``day`` (None: no maximum)."""
    return cap_rule(caps, day)[0]


@dataclass(frozen=True)
class Sinking:
    """A yearly payment saved up in equal parts from ``since`` to ``due``."""
    due: date
    amount: int
    since: date


def sinking_parts(sinking: list[Sinking], funding_days: list[date]) -> list[dict[date, int]]:
    """The part each funding day keeps for every yearly payment.

    The amount is split over the funding days in ``[since, due)``, the
    kopecks of the division going to the later parts.  A payment without
    such a day is not spread and is held on its date like any other bill.
    """
    result = []
    for item in sinking:
        days = [d for d in funding_days if item.since <= d < item.due]
        result.append({
            day: item.amount * (i + 1) // len(days) - item.amount * i // len(days)
            for i, day in enumerate(days)
        })
    return result


@dataclass
class LedgerInput:
    start: date
    today: date
    horizon: date        # last buffer-period start shown in the forecast
    data_end: date       # forecast data is complete through this day
    months: int          # look-ahead of every funding decision
    start_capital: int   # card + buffer money at the beginning of ``start``
    paydays: list[date]  # must extend past ``data_end``
    buffer_in: dict[date, int] = field(default_factory=dict)
    bills_out: dict[date, int] = field(default_factory=dict)
    card_in: dict[date, int] = field(default_factory=dict)       # spread over remaining days
    card_cover: dict[date, int] = field(default_factory=dict)    # covers the day's overspend
    card_out: dict[date, int] = field(default_factory=dict)      # expenses, bill overpayments
    card_to_piggy: dict[date, int] = field(default_factory=dict)
    stored_allocations: dict[date, int] = field(default_factory=dict)
    # Part of a stored allocation kept in the buffer by the daily maximum.
    stored_cap_kept: dict[date, int] = field(default_factory=dict)
    buffer_out: dict[date, int] = field(default_factory=dict)    # buffer -> piggy bank
    # Daily maximum by the day it takes effect, sorted: (since, cap or None,
    # excess to the piggy bank — optional, False by default).
    caps: list[tuple] = field(default_factory=list)
    # Buffer -> card top-ups of the current period after the cap was raised.
    cap_topups: dict[date, int] = field(default_factory=dict)
    # Buffer -> piggy bank: the current period's excess kept in the buffer,
    # moved when the excess was switched to the piggy bank.
    cap_moves: dict[date, int] = field(default_factory=dict)
    # A payday whose money is not confirmed yet: the card period it funds
    # gets nothing (used for the actual balances, not for the plan).
    unconfirmed_payday: date | None = None
    # Unpaid yearly payments, also beyond ``data_end`` (they are included
    # in ``bills_out`` only through it).
    sinking: list[Sinking] = field(default_factory=list)
    # «Минимум буфера»: money the card periods never get.
    buffer_floor: int = 0


@dataclass
class PeriodForecast:
    period: CardPeriod
    allocation: int
    opening: int   # card money at the start of the period (carry + allocation)
    daily: int
    carry: int     # left on the card after the last day


@dataclass
class LedgerResult:
    periods: list[CardPeriod]
    allocations: dict[date, int]
    buffer_end: dict[date, int]
    current: CardPeriod
    card_start_today: int
    today_limit: int
    available_today: int
    card_end_today: int
    spent_today: int
    tomorrow_limit: int
    overspend: int
    forecasts: dict[date, PeriodForecast]
    shortfall: int
    shortfall_date: date | None
    to_store: dict[date, tuple[date, int, int]]  # start -> (funded on, amount, kept by cap)
    cap_kept: dict[date, int] = field(default_factory=dict)   # by period start
    cap_trims: dict[date, int] = field(default_factory=dict)  # card -> buffer by day
    # «Избыток в копилку»: money above the maximum to the piggy bank by day,
    # and the part of it paid by the buffer on funding days.
    cap_to_piggy: dict[date, int] = field(default_factory=dict)
    piggy_from_buffer: dict[date, int] = field(default_factory=dict)
    cap_piggy_trims: dict[date, int] = field(default_factory=dict)  # card -> piggy by day
    # Card money left from the previous period on the first day of each
    # period, before the new money (negative: its overspend).
    carry_in: dict[date, int] = field(default_factory=dict)
    # The parts of ``cap_piggy_trims`` and ``cap_trims`` left on the card
    # from the previous period: on the first day of a period the leftover is
    # trimmed first.
    carry_piggy_trims: dict[date, int] = field(default_factory=dict)
    carry_buffer_trims: dict[date, int] = field(default_factory=dict)
    # Kept for yearly payments on each funding day, and in the buffer at the
    # end of each day (a part of ``buffer_end``).
    sinking_in: dict[date, int] = field(default_factory=dict)
    sinking_end: dict[date, int] = field(default_factory=dict)


def buffer_flows(inp: LedgerInput) -> dict[date, int]:
    """Buffer income minus obligatory payments and piggy-bank moves by date."""
    flows: dict[date, int] = {}
    for source, sign in ((inp.buffer_in, 1), (inp.bills_out, -1), (inp.buffer_out, -1)):
        for day, value in source.items():
            flows[day] = flows.get(day, 0) + sign * value
    return flows


def sinking_decisions(inp: LedgerInput, flows: dict[date, int], funding_days: set[date]):
    """Yearly payments.  For a funding decision the money already kept for
    them is not free, every later part is an outflow on its funding day,
    and the payment itself is then covered by the kept parts.

    Returns the decision flows, the parts kept on each funding day and
    ``kept_before(day)``: the money kept for payments due on or after ``day``.
    """
    funding_all = sorted(set(funding_days) | {p for p in inp.paydays if p > inp.data_end})
    parts = sinking_parts(inp.sinking, funding_all)
    decision_flows = dict(flows)
    sinking_in: dict[date, int] = {}
    for item, item_parts in zip(inp.sinking, parts):
        if not item_parts:
            continue
        decision_flows[item.due] = decision_flows.get(item.due, 0) + item.amount
        for day, value in item_parts.items():
            decision_flows[day] = decision_flows.get(day, 0) - value
            sinking_in[day] = sinking_in.get(day, 0) + value

    def kept_before(day: date) -> int:
        return sum(
            value
            for item, item_parts in zip(inp.sinking, parts) if item.due >= day
            for part_day, value in item_parts.items() if part_day < day
        )

    return decision_flows, sinking_in, kept_before


def run_ledger(inp: LedgerInput) -> LedgerResult:
    if not inp.start <= inp.today <= inp.horizon <= inp.data_end:
        raise ValueError("Неверный диапазон расчёта")
    periods = card_periods(inp.start, inp.paydays, inp.data_end)
    by_funding: dict[date, list[CardPeriod]] = {}
    for period in periods:
        by_funding.setdefault(period.funded_on, []).append(period)
    funded_days = {day: sum(p.days for p in items) for day, items in by_funding.items()}
    flows = buffer_flows(inp)

    decision_flows, sinking_in, kept_before = sinking_decisions(inp, flows, set(by_funding))

    # Buffer pass.  It never looks at card spending, except for the money the
    # daily maximum returns from the card (through today).
    buffer = inp.start_capital
    allocations: dict[date, int] = {}
    cap_kept: dict[date, int] = {}
    buffer_end: dict[date, int] = {}
    to_store: dict[date, tuple[date, int, int]] = {}
    starts = {period.start: period for period in periods}
    cap_days = {entry[0] for entry in inp.caps}
    cap_trims: dict[date, int] = {}
    cap_to_piggy: dict[date, int] = {}
    piggy_from_buffer: dict[date, int] = {}
    cap_piggy_trims: dict[date, int] = {}
    carry_in: dict[date, int] = {}
    carry_piggy_trims: dict[date, int] = {}
    carry_buffer_trims: dict[date, int] = {}
    active: CardPeriod | None = None
    card = 0
    card_start_today = 0
    opening_by_start: dict[date, int] = {}
    for day in daterange(inp.start, inp.data_end):
        funded_today = 0
        if day in by_funding:
            look_ahead = min(add_months(day, inp.months), inp.data_end)
            decision = safe_daily_rate(
                opening=buffer - kept_before(day) - inp.buffer_floor, start=day, end=look_ahead,
                flows=decision_flows,
                funded_days={d: n for d, n in funded_days.items() if day <= d <= look_ahead},
            )
            for period in by_funding[day]:
                # A stored period keeps its recorded amount, whether it was
                # locked in automatically because its funding day has passed
                # or explicitly frozen before an edit to a later payday.
                # Recalculating it is an explicit action (a new start, or an
                # edit at or before this period) that clears the stored row.
                kept = 0
                if period.funded_on == inp.unconfirmed_payday:
                    value = 0
                elif period.start in inp.stored_allocations:
                    value = inp.stored_allocations[period.start]
                    kept = inp.stored_cap_kept.get(period.start, 0)
                else:
                    daily = decision.daily
                    cap = cap_on(inp.caps, day)
                    if cap is not None and daily > cap:
                        kept, daily = (daily - cap) * period.days, cap
                    value = daily * period.days
                    if day <= inp.today:
                        to_store[period.start] = (period.funded_on, value, kept)
                allocations[period.start] = value
                cap_kept[period.start] = kept
                funded_today += value
                if kept and cap_rule(inp.caps, day)[1]:
                    funded_today += kept
                    piggy_from_buffer[day] = piggy_from_buffer.get(day, 0) + kept
                    cap_to_piggy[day] = cap_to_piggy.get(day, 0) + kept
        buffer += flows.get(day, 0) - funded_today

        # Card through today with real operations.
        if day <= inp.today:
            carried = 0
            if day in starts:
                active = starts[day]
                carry_in[day] = card
                carried = max(0, card)
                card += allocations[day]
            trim = 0
            cap, to_piggy = cap_rule(inp.caps, day)
            if (day == active.start or day in cap_days) and cap is not None:
                trim = max(0, card - cap * ((active.end - day).days + 1))
            topup = inp.cap_topups.get(day, 0)
            card += topup - trim
            buffer -= topup
            move = inp.cap_moves.get(day, 0)
            if move:
                buffer -= move
                piggy_from_buffer[day] = piggy_from_buffer.get(day, 0) + move
                cap_to_piggy[day] = cap_to_piggy.get(day, 0) + move
            if trim and to_piggy:
                cap_to_piggy[day] = cap_to_piggy.get(day, 0) + trim
                cap_piggy_trims[day] = trim
                if min(trim, carried):
                    carry_piggy_trims[day] = min(trim, carried)
            elif trim:
                buffer += trim
                cap_trims[day] = trim
                if min(trim, carried):
                    carry_buffer_trims[day] = min(trim, carried)
            if day in starts:
                opening_by_start[day] = card
            if day == inp.today:
                card_start_today = card
            card += (inp.card_in.get(day, 0) + inp.card_cover.get(day, 0)
                     - inp.card_out.get(day, 0) - inp.card_to_piggy.get(day, 0))
        buffer_end[day] = buffer
    sinking_end = {day: kept_before(day + ONE_DAY) for day in daterange(inp.start, inp.data_end)}

    current = next(p for p in periods if p.start <= inp.today <= p.end)
    days_left = (current.end - inp.today).days + 1
    today_limit = max(0, (card_start_today + inp.card_in.get(inp.today, 0)) // days_left)
    spent_today = inp.card_out.get(inp.today, 0)
    available_today = (today_limit + inp.card_cover.get(inp.today, 0)
                       - spent_today - inp.card_to_piggy.get(inp.today, 0))

    # Forecast of the card: the current period is spent evenly from tonight's
    # balance; every later period adds its allocation to the carried balance.
    forecasts: dict[date, PeriodForecast] = {}
    if days_left > 1:
        tomorrow_limit, carry = spend_evenly(card, days_left - 1)
    else:
        tomorrow_limit, carry = 0, card
    forecasts[current.start] = PeriodForecast(
        current, allocations[current.start], opening_by_start[current.start], today_limit, carry
    )
    for period in periods[periods.index(current) + 1:]:
        opening = carry + allocations[period.start]
        daily, carry = spend_evenly(opening, period.days)
        forecasts[period.start] = PeriodForecast(period, allocations[period.start], opening, daily, carry)
        if days_left == 1 and period.start == current.end + ONE_DAY:
            tomorrow_limit = daily

    lowest = 0
    lowest_date = None
    for day in daterange(inp.today, inp.data_end):
        if buffer_end[day] < lowest:
            lowest, lowest_date = buffer_end[day], day

    return LedgerResult(
        periods=periods,
        allocations=allocations,
        buffer_end=buffer_end,
        current=current,
        card_start_today=card_start_today,
        today_limit=today_limit,
        available_today=available_today,
        card_end_today=card,
        spent_today=spent_today,
        tomorrow_limit=tomorrow_limit,
        overspend=max(0, -available_today),
        forecasts=forecasts,
        shortfall=-lowest,
        shortfall_date=lowest_date,
        to_store=to_store,
        cap_kept=cap_kept,
        cap_trims=cap_trims,
        cap_to_piggy=cap_to_piggy,
        piggy_from_buffer=piggy_from_buffer,
        cap_piggy_trims=cap_piggy_trims,
        carry_in=carry_in,
        carry_piggy_trims=carry_piggy_trims,
        carry_buffer_trims=carry_buffer_trims,
        sinking_in=sinking_in,
        sinking_end=sinking_end,
    )


def safe_rate_from(inp: LedgerInput, result: LedgerResult, day: date, held: int = 0) -> int:
    """Largest equal daily rate for the rest of the current card period and
    every later one, as if the period were funded again on ``day`` from the
    buffer plus the ``held`` money it still has for its remaining days (a
    raised daily maximum)."""
    current = next(p for p in result.periods if p.start <= day <= p.end)
    look_ahead = min(add_months(day, inp.months), inp.data_end)
    funded_days = {day: (current.end - day).days + 1}
    for period in result.periods:
        if period.start > current.start and day <= period.funded_on <= look_ahead:
            funded_days[period.funded_on] = funded_days.get(period.funded_on, 0) + period.days
    decision_flows, _, kept_before = sinking_decisions(
        inp, buffer_flows(inp), {p.funded_on for p in result.periods})
    opening = held - kept_before(day) - inp.buffer_floor + (
        inp.start_capital if day == inp.start else result.buffer_end[day - ONE_DAY])
    return safe_daily_rate(
        opening=opening, start=day, end=look_ahead, flows=decision_flows, funded_days=funded_days,
    ).daily


def spare_buffer(inp: LedgerInput, result: LedgerResult) -> int:
    """Buffer money that can leave today (to the piggy bank) without changing
    any card period or leaving an obligation uncovered, in whole rubles."""
    if result.shortfall > 0:
        return 0
    # Money kept for yearly payments and the minimum are never spare.
    lowest = min(result.buffer_end[d] - result.sinking_end.get(d, 0)
                 for d in daterange(inp.today, inp.data_end)) - inp.buffer_floor
    if lowest <= 0:
        return 0
    stored = {p.start: result.allocations[p.start] for p in result.periods if p.funded_on <= inp.today}
    kept = {start: result.cap_kept.get(start, 0) for start in stored}
    later = {p.start: result.allocations[p.start] for p in result.periods if p.funded_on > inp.today}

    def keeps(value: int) -> bool:
        out = dict(inp.buffer_out)
        out[inp.today] = out.get(inp.today, 0) + value
        other = run_ledger(replace(inp, buffer_out=out, stored_allocations=stored, stored_cap_kept=kept))
        return other.shortfall == 0 and all(other.allocations[s] == v for s, v in later.items())

    low, high = 0, lowest // 100
    if keeps(high * 100):
        return high * 100
    while high - low > 1:
        middle = (low + high) // 2
        if keeps(middle * 100):
            low = middle
        else:
            high = middle
    return low * 100


@dataclass(frozen=True)
class BufferRow:
    start: date
    end: date
    payday: date | None
    received: int
    mandatory: int
    to_card: int
    buffer_start: int
    buffer_end: int
    lowest: int
    funded: PeriodForecast | None
    to_piggy: int = 0   # buffer money moved to the piggy bank
    card_to_piggy: int = 0  # the period's card money above the maximum moved to the piggy bank
    card_days: int = 0  # days of the card periods funded in this row


def buffer_rows(inp: LedgerInput, result: LedgerResult, *, past: bool = False) -> list[BufferRow]:
    """Buffer periods by actual paydays, from the current one to the horizon.
    With ``past`` the closed periods from the calculation start up to (but not
    including) the current one."""
    paydays = sorted(set(inp.paydays))
    previous = [p for p in paydays if p <= inp.today]
    first = max(inp.start, previous[-1]) if previous else inp.start
    if past:
        starts = [inp.start] + [p for p in paydays if inp.start < p < first] if inp.start < first else []
    else:
        starts = [first] + [p for p in paydays if first < p <= inp.horizon]
    card_piggy = {d: v - result.carry_piggy_trims.get(d, 0) for d, v in result.cap_piggy_trims.items()}
    rows: list[BufferRow] = []
    for row_start in starts:
        next_payday = next(p for p in paydays if p > row_start)
        row_end = min(next_payday - ONE_DAY, inp.data_end)
        days = list(daterange(row_start, row_end))
        funded = [f for f in result.forecasts.values() if f.period.funded_on == row_start]
        rows.append(BufferRow(
            start=row_start,
            end=row_end,
            payday=row_start if row_start in paydays else None,
            received=sum(inp.buffer_in.get(d, 0) for d in days),
            mandatory=sum(inp.bills_out.get(d, 0) for d in days),
            # Net money to the card: the daily maximum may return some of it
            # to the buffer or move it to the piggy bank, or top the card up.
            # The previous period's leftover moved to the piggy bank was never
            # this period's money: it is in neither column.
            to_card=sum(result.allocations[p.start] for p in result.periods
                        if row_start <= p.funded_on <= row_end)
            - sum(result.cap_trims.get(d, 0) + card_piggy.get(d, 0) for d in days)
            + sum(inp.cap_topups.get(d, 0) for d in days),
            card_to_piggy=sum(card_piggy.get(d, 0) for d in days),
            to_piggy=sum(inp.buffer_out.get(d, 0) + result.piggy_from_buffer.get(d, 0) for d in days),
            card_days=sum(p.days for p in result.periods if row_start <= p.funded_on <= row_end),
            buffer_start=(inp.start_capital if row_start == inp.start
                          else result.buffer_end[row_start - ONE_DAY]),
            buffer_end=result.buffer_end[row_end],
            lowest=min(result.buffer_end[d] for d in days),
            funded=max(funded, key=lambda f: f.period.start) if funded else None,
        ))
    return rows
