"""Rules of the card / buffer / piggy-bank model (amounts in kopecks)."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.ledger import (
    CardPeriod, LedgerInput, buffer_rows, card_periods, run_ledger, safe_daily_rate, safe_rate_from,
    spare_buffer, spend_evenly,
)

RUB = 100
D = date
PAYDAYS = [D(2026, 8, 21), D(2026, 9, 7), D(2026, 9, 22), D(2026, 10, 7), D(2026, 10, 22),
           D(2026, 11, 6), D(2026, 11, 20), D(2026, 12, 7), D(2026, 12, 22), D(2027, 1, 7)]


# Plenty of future income, so a 1 000 ₽ card for ten days is limited only by
# today's money: 100 ₽ a day.
RICH = {D(2026, 9, 22): 100000 * RUB, D(2026, 10, 7): 100000 * RUB, D(2026, 10, 22): 100000 * RUB}


def ten_days_with_1000(today=D(2026, 9, 13), **overrides):
    return ledger(today, start=D(2026, 9, 13),
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


# «Максимум в день».

def test_daily_cap_keeps_the_excess_in_the_buffer():
    plain = run_ledger(ten_days_with_1000())
    capped = run_ledger(ten_days_with_1000(caps=[(D(2026, 9, 13), 80 * RUB)]))
    start = D(2026, 9, 13)
    assert plain.allocations[start] == 1000 * RUB
    assert capped.allocations[start] == 800 * RUB
    assert capped.cap_kept[start] == 200 * RUB
    assert capped.today_limit == 80 * RUB
    assert capped.buffer_end[start] == plain.buffer_end[start] + 200 * RUB
    # Every later period is capped as well, the rest stays in the buffer.
    for period in capped.periods[1:]:
        assert capped.allocations[period.start] == 80 * RUB * period.days


def test_daily_cap_above_the_rate_changes_nothing():
    plain = run_ledger(ten_days_with_1000())
    high = run_ledger(ten_days_with_1000(caps=[(D(2026, 9, 13), 150 * RUB)]))
    start = D(2026, 9, 13)
    assert high.allocations[start] == plain.allocations[start]
    assert high.cap_kept[start] == 0 and not high.cap_trims
    assert high.today_limit == plain.today_limit


def test_daily_cap_trims_money_left_from_the_previous_period():
    # Nothing is spent from 13 to 22 September: the 1 000 ₽ are carried over.
    cap = 2000 * RUB
    inp = ten_days_with_1000(today=D(2026, 9, 23), caps=[(D(2026, 9, 13), cap)])
    result = run_ledger(inp)
    nxt = D(2026, 9, 23)
    assert result.allocations[nxt] == cap * 15
    # Option «а»: the cap holds for the whole daily amount, the carried
    # 1 000 ₽ go back to the buffer on the first day of the period.
    assert result.cap_trims == {nxt: 1000 * RUB}
    assert result.carry_in[nxt] == 1000 * RUB
    assert result.carry_buffer_trims == {nxt: 1000 * RUB} and not result.carry_piggy_trims
    assert result.card_start_today == cap * 15
    assert result.today_limit == cap
    plain = run_ledger(replace(inp, caps=[]))
    assert plain.today_limit > cap


def test_daily_cap_lowered_mid_period_returns_the_rest_of_the_card():
    spent = {D(2026, 9, d): 100 * RUB for d in range(13, 18)}
    inp = ten_days_with_1000(today=D(2026, 9, 18), card_out=spent)
    before = run_ledger(inp)
    after = run_ledger(replace(inp, caps=[(D(2026, 9, 18), 80 * RUB)]))
    # 500 ₽ left for five days (100 ₽ a day) -> at most 400 ₽.
    assert before.today_limit == 100 * RUB
    assert after.today_limit == 80 * RUB
    assert after.cap_trims == {D(2026, 9, 18): 100 * RUB}
    assert after.card_start_today == 400 * RUB
    # The days already gone and the period's money are not rewritten.
    assert after.allocations[D(2026, 9, 13)] == before.allocations[D(2026, 9, 13)]
    for day in range(13, 18):
        assert after.buffer_end[D(2026, 9, day)] == before.buffer_end[D(2026, 9, day)]
    assert after.buffer_end[D(2026, 9, 18)] == before.buffer_end[D(2026, 9, 18)] + 100 * RUB


def test_daily_cap_below_the_card_balance_per_day_changes_nothing_mid_period():
    spent = {D(2026, 9, d): 150 * RUB for d in range(13, 18)}  # 250 ₽ for five days
    inp = ten_days_with_1000(today=D(2026, 9, 18), card_out=spent, caps=[(D(2026, 9, 18), 80 * RUB)])
    result = run_ledger(inp)
    assert not result.cap_trims
    assert result.today_limit == 50 * RUB


def test_raised_cap_tops_up_only_within_the_safe_rate():
    caps = [(D(2026, 9, 13), 80 * RUB)]
    inp = ten_days_with_1000(today=D(2026, 9, 14), caps=caps, card_out={D(2026, 9, 13): 80 * RUB})
    result = run_ledger(inp)
    # 200 ₽ kept in the buffer + 80 ₽ x 9 days still on the card.
    rate = safe_rate_from(inp, result, D(2026, 9, 14), held=80 * RUB * 9)
    assert rate == (200 * RUB + 720 * RUB) // 9
    topped = run_ledger(replace(inp, caps=caps + [(D(2026, 9, 14), 150 * RUB)],
                                cap_topups={D(2026, 9, 14): (rate - 80 * RUB) * 9}))
    assert topped.today_limit == rate
    assert topped.buffer_end[D(2026, 9, 14)] >= 0


def test_spare_buffer_never_changes_card_periods():
    inp = ten_days_with_1000(caps=[(D(2026, 9, 13), 80 * RUB)])
    result = run_ledger(inp)
    spare = spare_buffer(inp, result)
    assert spare > 0 and spare % RUB == 0
    moved = run_ledger(replace(inp, buffer_out={D(2026, 9, 13): spare},
                               stored_allocations={D(2026, 9, 13): result.allocations[D(2026, 9, 13)]}))
    assert moved.allocations == result.allocations
    assert moved.shortfall == 0
    more = run_ledger(replace(inp, buffer_out={D(2026, 9, 13): spare + RUB},
                              stored_allocations={D(2026, 9, 13): result.allocations[D(2026, 9, 13)]}))
    assert more.allocations != result.allocations or more.shortfall > 0


def yearly_ledger(**overrides):
    from app.ledger import Sinking
    paydays = [p for p in PAYDAYS if p >= D(2026, 9, 22)]
    values = dict(
        start_capital=30000 * RUB, paydays=PAYDAYS,
        buffer_in={p: 30000 * RUB for p in paydays if p <= D(2026, 11, 8)},
        sinking=[Sinking(D(2027, 1, 10), 9000 * RUB, D(2026, 9, 8))],
    )
    values.update(overrides)
    return ledger(D(2026, 9, 8), **values)


def test_yearly_payment_is_saved_up_in_equal_parts_on_every_funding_day():
    result = run_ledger(yearly_ledger())
    # 8 Sep (the start) and the paydays of 22 Sep … 7 Jan: nine parts.
    funding = [D(2026, 9, 8), D(2026, 9, 22), D(2026, 10, 7), D(2026, 10, 22), D(2026, 11, 6)]
    assert result.sinking_in == {day: 1000 * RUB for day in funding + [
        D(2026, 11, 20), D(2026, 12, 7), D(2026, 12, 22), D(2027, 1, 7)]}
    assert result.sinking_end[D(2026, 11, 8)] == 5000 * RUB
    # Every card period gives up the same 1 000 ₽ part.
    plain = run_ledger(yearly_ledger(sinking=[]))
    for period in result.periods:
        given_up = plain.allocations[period.start] - result.allocations[period.start]
        assert abs(given_up - 1000 * RUB) <= period.days
    # The buffer always holds at least what is saved up.
    assert all(result.buffer_end[d] >= kept for d, kept in result.sinking_end.items())


def test_saved_up_yearly_payment_is_not_paid_twice():
    from app.ledger import Sinking
    # Due inside the forecast: held by the parts, not again on its date.
    due = D(2026, 10, 30)
    inp = yearly_ledger(sinking=[Sinking(due, 3000 * RUB, D(2026, 9, 8))], bills_out={due: 3000 * RUB})
    result = run_ledger(inp)
    assert result.sinking_in == {D(2026, 9, 8): 750 * RUB, D(2026, 9, 22): 750 * RUB,
                                 D(2026, 10, 7): 750 * RUB, D(2026, 10, 22): 750 * RUB}
    assert result.sinking_end[due - timedelta(days=1)] == 3000 * RUB
    assert result.sinking_end[due] == 0
    assert result.buffer_end[due] >= 0


def test_money_saved_for_yearly_payments_is_never_spare():
    from app.ledger import Sinking
    caps = [(D(2026, 9, 8), 100 * RUB)]
    plain = yearly_ledger(caps=caps, sinking=[])
    yearly = yearly_ledger(caps=caps)
    spare_plain = spare_buffer(plain, run_ledger(plain))
    result = run_ledger(yearly)
    spare = spare_buffer(yearly, result)
    assert spare_plain > 0
    # The saved part stays in the buffer, the rest is still offered.
    assert spare <= spare_plain - result.sinking_end[D(2026, 9, 8)]
    moved = run_ledger(replace(yearly, buffer_out={D(2026, 9, 8): spare},
                               stored_allocations={D(2026, 9, 8): result.allocations[D(2026, 9, 8)]}))
    assert moved.allocations == result.allocations
    assert all(moved.buffer_end[d] >= kept for d, kept in moved.sinking_end.items())


def test_excess_to_piggy_leaves_the_buffer_as_without_the_cap():
    plain = run_ledger(ten_days_with_1000())
    piggy = run_ledger(ten_days_with_1000(caps=[(D(2026, 9, 13), 80 * RUB, True)]))
    start = D(2026, 9, 13)
    assert piggy.allocations[start] == 800 * RUB
    assert piggy.today_limit == 80 * RUB
    # The buffer pays the period in full: 800 ₽ to the card, 200 ₽ to the piggy bank.
    assert piggy.buffer_end[start] == plain.buffer_end[start]
    assert piggy.piggy_from_buffer[start] == 200 * RUB
    assert piggy.cap_to_piggy[start] == 200 * RUB
    assert piggy.shortfall == 0


def test_excess_to_piggy_trims_the_card_into_the_piggy_bank():
    spent = {D(2026, 9, d): 100 * RUB for d in range(13, 18)}
    inp = ten_days_with_1000(today=D(2026, 9, 18), card_out=spent)
    before = run_ledger(inp)
    after = run_ledger(replace(inp, caps=[(D(2026, 9, 18), 80 * RUB, True)]))
    assert after.today_limit == 80 * RUB
    assert after.cap_piggy_trims == {D(2026, 9, 18): 100 * RUB}
    assert not after.cap_trims
    assert after.buffer_end[D(2026, 9, 18)] == before.buffer_end[D(2026, 9, 18)]
    # Trimmed mid-period: the period's own money, not a leftover.
    assert not after.carry_piggy_trims


def test_leftover_moved_to_piggy_is_not_taken_from_the_new_periods_card_money():
    # Nothing is spent from 13 to 22 September: the 1 000 ₽ are carried over
    # and go to the piggy bank on the first day of the next period.
    nxt = D(2026, 9, 23)
    inp = ten_days_with_1000(today=nxt, caps=[(D(2026, 9, 13), 2000 * RUB, True)])
    result = run_ledger(inp)
    assert result.allocations[nxt] == 2000 * RUB * 15
    assert result.carry_in[nxt] == 1000 * RUB
    assert result.cap_piggy_trims == result.carry_piggy_trims == {nxt: 1000 * RUB}
    assert not result.carry_buffer_trims
    row = next(r for r in buffer_rows(inp, result) if r.start == D(2026, 9, 22))
    assert row.to_card == 2000 * RUB * 15
    assert row.card_to_piggy == 0


def test_lowered_cap_on_the_first_day_trims_the_leftover_first():
    nxt = D(2026, 9, 23)
    inp = ten_days_with_1000(today=nxt, caps=[(nxt, 1500 * RUB, True)])
    result = run_ledger(inp)
    # The period was funded without the maximum, above 1 500 ₽ a day.
    assert result.allocations[nxt] > 1500 * RUB * 15
    assert result.carry_piggy_trims == {nxt: 1000 * RUB}
    own = result.cap_piggy_trims[nxt] - 1000 * RUB
    row = next(r for r in buffer_rows(inp, result) if r.start == D(2026, 9, 22))
    assert row.to_card == 1500 * RUB * 15
    assert row.card_to_piggy == own == result.allocations[nxt] - 1500 * RUB * 15


# «Минимум буфера».

def test_buffer_floor_is_never_given_to_the_card():
    # 1 000 ₽ for ten days, 400 ₽ of them must stay: 60 ₽ a day.
    inp = ten_days_with_1000(buffer_floor=400 * RUB)
    result = run_ledger(inp)
    assert result.allocations[D(2026, 9, 13)] == 600 * RUB
    assert result.today_limit == 60 * RUB
    assert result.buffer_end[D(2026, 9, 13)] == 400 * RUB


def test_buffer_never_goes_below_the_floor_after_obligatory_payments():
    # A big salary on the 7th, a small advance and the rent on the 22nd: with
    # a minimum of 2 000 ₽ the buffer bottoms out at 2 000 ₽ instead of zero.
    paydays = [D(2026, 9, 22), D(2026, 10, 7), D(2026, 10, 22), D(2026, 11, 6), D(2026, 11, 20),
               D(2026, 12, 7), D(2026, 12, 22), D(2027, 1, 7), D(2027, 1, 22), D(2027, 2, 6), D(2027, 2, 20)]
    values = dict(
        start=D(2026, 10, 7), horizon=D(2026, 12, 7), data_end=D(2027, 2, 7), months=2, paydays=paydays,
        buffer_in={p: (58000 if p.day == 7 else 43000) * RUB for p in paydays},
        bills_out={p: (15400 if p.day == 7 else 27700) * RUB for p in paydays},
    )
    plain = run_ledger(ledger(D(2026, 10, 7), **values))
    result = run_ledger(ledger(D(2026, 10, 7), buffer_floor=2000 * RUB, **values))
    assert min(plain.buffer_end.values()) < 2000 * RUB
    assert 2000 * RUB <= min(result.buffer_end.values()) < 2001 * RUB
    assert result.shortfall == 0


def test_buffer_floor_holds_through_weak_periods():
    # 30 000 ₽ now, nothing on 7 October, 30 000 ₽ on 22 October.
    values = dict(start=D(2026, 9, 22), start_capital=0, months=2, data_end=D(2026, 12, 1),
                  buffer_in={D(2026, 9, 22): 30000 * RUB, D(2026, 10, 22): 30000 * RUB})
    plain = run_ledger(ledger(D(2026, 9, 22), **values))
    inp = ledger(D(2026, 9, 22), buffer_floor=6000 * RUB, **values)
    result = run_ledger(inp)
    assert all(result.buffer_end[d] >= 6000 * RUB for d in result.buffer_end)
    assert result.forecasts[D(2026, 9, 23)].daily < plain.forecasts[D(2026, 9, 23)].daily
    assert result.shortfall == 0


def test_obligatory_payments_may_take_the_buffer_below_the_floor():
    inp = ledger(D(2026, 9, 10), start_capital=10000 * RUB, bills_out={D(2026, 9, 20): 5000 * RUB},
                 buffer_floor=8000 * RUB)
    result = run_ledger(inp)
    # The card gets nothing, the payment is still made and nothing is short.
    assert result.allocations[D(2026, 9, 8)] == 0
    assert result.buffer_end[D(2026, 9, 20)] == 5000 * RUB
    assert result.shortfall == 0


def test_spare_buffer_never_goes_below_the_floor():
    caps = [(D(2026, 9, 13), 80 * RUB)]
    plain = ten_days_with_1000(caps=caps)
    inp = ten_days_with_1000(caps=caps, buffer_floor=100 * RUB)
    result = run_ledger(inp)
    spare = spare_buffer(inp, result)
    assert 0 < spare <= spare_buffer(plain, run_ledger(plain)) - 100 * RUB
    moved = run_ledger(replace(inp, buffer_out={D(2026, 9, 13): spare},
                               stored_allocations={D(2026, 9, 13): result.allocations[D(2026, 9, 13)]}))
    assert all(moved.buffer_end[d] >= 100 * RUB for d in moved.buffer_end if d >= inp.today)


def test_buffer_floor_comes_on_top_of_yearly_savings():
    result = run_ledger(yearly_ledger(buffer_floor=2000 * RUB))
    assert all(result.buffer_end[d] - kept >= 2000 * RUB for d, kept in result.sinking_end.items())


def test_safe_rate_keeps_the_floor():
    inp = ten_days_with_1000(today=D(2026, 9, 14), buffer_floor=400 * RUB,
                             card_out={D(2026, 9, 13): 60 * RUB})
    result = run_ledger(inp)
    # Only the 540 ₽ left on the card count: the 400 ₽ in the buffer stay.
    assert safe_rate_from(inp, result, D(2026, 9, 14), held=540 * RUB) == 60 * RUB


def test_carry_in_is_the_previous_periods_leftover_or_overspend():
    nxt = D(2026, 9, 23)
    spent = {D(2026, 9, 22): 1300 * RUB}
    result = run_ledger(ten_days_with_1000(today=nxt, card_out=spent))
    assert result.carry_in[nxt] == -300 * RUB
    assert result.card_start_today == result.allocations[nxt] - 300 * RUB
