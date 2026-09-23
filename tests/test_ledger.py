"""Rules of the card / buffer / piggy-bank model (amounts in kopecks)."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.ledger import (
    CardPeriod, LedgerInput, buffer_rows, card_periods, run_ledger, safe_daily_rate, spend_evenly,
)

RUB = 100
D = date
PAYDAYS = [D(2026, 8, 21), D(2026, 9, 7), D(2026, 9, 22), D(2026, 10, 7), D(2026, 10, 22),
           D(2026, 11, 6), D(2026, 11, 20), D(2026, 12, 7), D(2026, 12, 22), D(2027, 1, 7)]


# Plenty of future income, so a 1 000 ₽ card for ten days is limited only by
# today's money: 100 ₽ a day.
RICH = {D(2026, 9, 22): 100000 * RUB, D(2026, 10, 7): 100000 * RUB, D(2026, 10, 22): 100000 * RUB}


def ten_days_with_1000(**overrides):
    return ledger(D(2026, 9, 13), start=D(2026, 9, 13),
                  buffer_in={D(2026, 9, 13): 1000 * RUB, **RICH}, **overrides)


def ledger(today, **overrides):
    values = dict(
        start=D(2026, 9, 8), today=today, horizon=D(2026, 10, 8), data_end=D(2026, 11, 8), months=1,
        start_capital=0, paydays=PAYDAYS,
    )
    values.update(overrides)
    return LedgerInput(**values)


def test_card_period_includes_payday_and_starts_next_day():
    periods = card_periods(D(2026, 9, 8), PAYDAYS, D(2026, 10, 30))
    assert periods[0] == CardPeriod(D(2026, 9, 8), D(2026, 9, 22), D(2026, 9, 8))
    assert periods[1] == CardPeriod(D(2026, 9, 23), D(2026, 10, 7), D(2026, 9, 22))
    assert periods[1].days == 15


def test_buffer_rows_start_on_the_actual_payday():
    inp = ledger(D(2026, 9, 22), start_capital=15000 * RUB,
                 buffer_in={D(2026, 9, 22): 30000 * RUB, D(2026, 10, 7): 30000 * RUB})
    rows = buffer_rows(inp, run_ledger(inp))
    assert [(r.start, r.end) for r in rows[:2]] == [
        (D(2026, 9, 22), D(2026, 10, 6)),
        (D(2026, 10, 7), D(2026, 10, 21)),
    ]
    assert rows[0].received == 30000 * RUB
    # The money received on 22 September funds the card from 23 September.
    assert rows[0].funded.period.start == D(2026, 9, 23)


def test_bill_on_payday_is_covered_by_the_payment_of_that_day():
    # Without the advance on the 22nd the buffer could not hold 20 000 ₽.
    inp = ledger(D(2026, 9, 10), start_capital=15000 * RUB,
                 buffer_in={D(2026, 9, 22): 30000 * RUB, D(2026, 10, 7): 30000 * RUB},
                 bills_out={D(2026, 9, 22): 20000 * RUB})
    result = run_ledger(inp)
    assert result.shortfall == 0
    assert min(result.buffer_end.values()) >= 0
    row = buffer_rows(inp, result)[1]
    assert (row.start, row.mandatory) == (D(2026, 9, 22), 20000 * RUB)


def test_buffer_holds_bills_on_exact_dates_and_is_not_in_daily_money():
    base = ledger(D(2026, 9, 10), start_capital=15000 * RUB,
                  buffer_in={D(2026, 9, 22): 30000 * RUB},
                  bills_out={D(2026, 9, 30): 9000 * RUB})
    result = run_ledger(base)
    # Card money is fixed at funding; the buffer is never divided into days.
    card = result.allocations[D(2026, 9, 8)]
    assert result.today_limit == card // 13
    assert result.buffer_end[D(2026, 9, 10)] == 15000 * RUB - card
    assert result.buffer_end[D(2026, 9, 10)] > 0
    assert result.today_limit * 13 <= card


def test_overspend_example_from_specification_does_not_touch_buffer():
    # Card: 1 000 ₽ for the last ten days (limit 100 ₽); 1 100 ₽ spent today.
    inp = ten_days_with_1000(card_out={D(2026, 9, 13): 1100 * RUB})
    result = run_ledger(inp)
    assert result.current.days == 10
    assert result.today_limit == 100 * RUB
    assert result.available_today == -1000 * RUB
    assert result.overspend == 1000 * RUB
    no_spend = run_ledger(replace(inp, card_out={}))
    assert result.buffer_end == no_spend.buffer_end
    # Variant 1 – spread over the remaining days: the card is 100 ₽ short, so
    # the next nine days get nothing and the rest reduces the next period.
    assert result.tomorrow_limit == 0
    assert result.forecasts[result.current.start].carry == -100 * RUB


def test_covering_overspend_from_piggy_restores_following_days():
    inp = ten_days_with_1000(card_out={D(2026, 9, 13): 1100 * RUB},
                             card_cover={D(2026, 9, 13): 1000 * RUB})
    result = run_ledger(inp)
    assert result.today_limit == 100 * RUB
    assert result.available_today == 0
    assert result.tomorrow_limit == 100 * RUB


def test_spreading_overspend_lowers_only_remaining_days_of_current_period():
    inp = ten_days_with_1000(card_out={D(2026, 9, 13): 190 * RUB})
    result = run_ledger(inp)
    assert result.available_today == -90 * RUB
    assert result.tomorrow_limit == 90 * RUB  # 810 ₽ over nine days
    assert result.forecasts[result.current.start].carry == 0


def test_overspend_on_last_day_reduces_the_next_period():
    common = dict(start=D(2026, 9, 8), start_capital=15000 * RUB,
                  buffer_in={D(2026, 9, 22): 30000 * RUB, D(2026, 10, 7): 30000 * RUB})
    plain = run_ledger(ledger(D(2026, 9, 22), **common))
    overspent = run_ledger(ledger(D(2026, 9, 22), **common, card_out={D(2026, 9, 22): 3000 * RUB}))
    assert overspent.current.end == D(2026, 9, 22)  # the payday is the last card day
    assert overspent.available_today == plain.available_today - 3000 * RUB
    nxt = D(2026, 9, 23)
    # Salary money for the next period is the same, the buffer is the same …
    assert overspent.allocations[nxt] == plain.allocations[nxt]
    assert overspent.buffer_end == plain.buffer_end
    # … but it is added to the already reduced card, so from tomorrow the
    # limit is lower.
    assert overspent.forecasts[nxt].opening == plain.forecasts[nxt].opening - 3000 * RUB
    assert overspent.tomorrow_limit < plain.tomorrow_limit
    assert overspent.tomorrow_limit == overspent.forecasts[nxt].daily


def test_piggy_to_card_transfer_is_spread_over_remaining_days():
    inp = ten_days_with_1000()
    before = run_ledger(inp)
    after = run_ledger(replace(inp, card_in={D(2026, 9, 13): 900 * RUB},
                               card_out={D(2026, 9, 13): 190 * RUB}))
    assert (before.today_limit, after.today_limit) == (100 * RUB, 190 * RUB)
    assert after.available_today == 0
    assert after.tomorrow_limit == 190 * RUB
    assert after.buffer_end == before.buffer_end


def test_transfer_to_piggy_reduces_only_today():
    inp = ten_days_with_1000(card_to_piggy={D(2026, 9, 13): 100 * RUB})
    result = run_ledger(inp)
    assert result.available_today == 0
    assert result.tomorrow_limit == 100 * RUB


def test_weak_future_period_is_prefunded_by_the_buffer():
    # 30 000 ₽ now, nothing on 7 October, 30 000 ₽ on 22 October.
    inp = ledger(D(2026, 9, 22), start=D(2026, 9, 22), start_capital=0, months=2,
                 data_end=D(2026, 12, 1),
                 buffer_in={D(2026, 9, 22): 30000 * RUB, D(2026, 10, 22): 30000 * RUB})
    result = run_ledger(inp)
    first, second = result.allocations[D(2026, 9, 23)], result.allocations[D(2026, 10, 8)]
    assert first // 15 == second // 15  # the same daily amount in both periods
    assert result.buffer_end[D(2026, 10, 7)] == 30000 * RUB - first - second - result.allocations[D(2026, 9, 22)]
    assert result.shortfall == 0


def test_shortfall_is_reported_when_obligations_cannot_be_covered():
    inp = ledger(D(2026, 9, 10), start_capital=1000 * RUB, bills_out={D(2026, 9, 20): 5000 * RUB})
    result = run_ledger(inp)
    assert result.today_limit == 0
    assert (result.shortfall, result.shortfall_date) == (4000 * RUB, D(2026, 9, 20))


def test_closed_periods_keep_their_stored_allocation():
    inp = ledger(D(2026, 9, 25), buffer_in={D(2026, 9, 8): 15000 * RUB, D(2026, 9, 22): 30000 * RUB})
    fresh = run_ledger(inp)
    frozen = run_ledger(replace(inp, stored_allocations={D(2026, 9, 8): 7500 * RUB}))
    assert fresh.allocations[D(2026, 9, 8)] != 7500 * RUB
    assert frozen.allocations[D(2026, 9, 8)] == 7500 * RUB
    assert D(2026, 9, 8) not in frozen.to_store
    # The current period is recalculated and stored.
    assert D(2026, 9, 23) in frozen.to_store


def test_safe_rate_and_even_spending_helpers():
    decision = safe_daily_rate(opening=600, start=D(2026, 9, 1), end=D(2026, 9, 5),
                               flows={D(2026, 9, 3): -500}, funded_days={D(2026, 9, 1): 5})
    assert (decision.daily, decision.shortfall) == (20, 0)
    assert spend_evenly(1000, 3) == (333, 0)
    assert spend_evenly(-50, 3) == (0, -50)


@pytest.mark.parametrize("day", [D(2026, 9, 21), D(2026, 9, 22)])
def test_payday_money_is_not_spendable_before_next_day(day):
    inp = ledger(day, start=D(2026, 9, 21), buffer_in={D(2026, 9, 22): 30000 * RUB})
    result = run_ledger(inp)
    assert result.card_end_today == 0
    assert result.today_limit == 0
    if day == D(2026, 9, 22):
        assert result.tomorrow_limit > 0
