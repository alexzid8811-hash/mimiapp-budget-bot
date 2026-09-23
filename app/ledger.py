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
* The card balance is carried over.  An overspend reduces the remaining days
  of the current card period; on the last day it reduces the next period, whose
  new money is added to the already reduced card balance.
* The piggy bank changes only by explicit operations.  A piggy -> card
  transfer either increases the period (spread over the remaining days) or
  covers today's overspend directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    to_store: dict[date, tuple[date, int]]


def run_ledger(inp: LedgerInput) -> LedgerResult:
    if not inp.start <= inp.today <= inp.horizon <= inp.data_end:
        raise ValueError("Неверный диапазон расчёта")
    periods = card_periods(inp.start, inp.paydays, inp.data_end)
    by_funding: dict[date, list[CardPeriod]] = {}
    for period in periods:
        by_funding.setdefault(period.funded_on, []).append(period)
    funded_days = {day: sum(p.days for p in items) for day, items in by_funding.items()}
    flows: dict[date, int] = {}
    for source, sign in ((inp.buffer_in, 1), (inp.bills_out, -1)):
        for day, value in source.items():
            flows[day] = flows.get(day, 0) + sign * value

    # Buffer pass.  It never looks at card spending.
    buffer = inp.start_capital
    allocations: dict[date, int] = {}
    buffer_end: dict[date, int] = {}
    to_store: dict[date, tuple[date, int]] = {}
    for day in daterange(inp.start, inp.data_end):
        funded_today = 0
        if day in by_funding:
            look_ahead = min(add_months(day, inp.months), inp.data_end)
            decision = safe_daily_rate(
                opening=buffer, start=day, end=look_ahead, flows=flows,
                funded_days={d: n for d, n in funded_days.items() if day <= d <= look_ahead},
            )
            for period in by_funding[day]:
                # Money already moved to the card stays there: a period funded
                # on or before today keeps its stored amount.  Corrections that
                # must recalculate it drop the stored rows explicitly.
                funded = period.funded_on <= inp.today
                if funded and period.start in inp.stored_allocations:
                    value = inp.stored_allocations[period.start]
                else:
                    value = decision.daily * period.days
                    if day <= inp.today:
                        to_store[period.start] = (period.funded_on, value)
                allocations[period.start] = value
                funded_today += value
        buffer += flows.get(day, 0) - funded_today
        buffer_end[day] = buffer

    # Card pass through today with real operations.
    starts = {period.start: period for period in periods}
    card = 0
    card_start_today = 0
    opening_by_start: dict[date, int] = {}
    for day in daterange(inp.start, inp.today):
        if day in starts:
            card += allocations[day]
            opening_by_start[day] = card
        if day == inp.today:
            card_start_today = card
        card += (inp.card_in.get(day, 0) + inp.card_cover.get(day, 0)
                 - inp.card_out.get(day, 0) - inp.card_to_piggy.get(day, 0))

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
    )


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


def buffer_rows(inp: LedgerInput, result: LedgerResult) -> list[BufferRow]:
    """Buffer periods by actual paydays, from the current one to the horizon."""
    paydays = sorted(set(inp.paydays))
    previous = [p for p in paydays if p <= inp.today]
    first = max(inp.start, previous[-1]) if previous else inp.start
    starts = [first] + [p for p in paydays if first < p <= inp.horizon]
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
            to_card=sum(result.allocations[p.start] for p in result.periods
                        if row_start <= p.funded_on <= row_end),
            buffer_start=(inp.start_capital if row_start == inp.start
                          else result.buffer_end[row_start - ONE_DAY]),
            buffer_end=result.buffer_end[row_end],
            lowest=min(result.buffer_end[d] for d in days),
            funded=max(funded, key=lambda f: f.period.start) if funded else None,
        ))
    return rows
