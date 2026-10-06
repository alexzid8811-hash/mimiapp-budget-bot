"""Rules of the irregular-income model (amounts in kopecks)."""
from datetime import date, timedelta

from app.irregular import (
    Bill, Income, IrregularInput, IrregularSettings, ReserveMove, replay, reserve_share, split_income,
)

RUB = 100
D = date
SETTINGS = IrregularSettings(reserve_percent=10, stretch_days=14, lookahead_days=30)


def bills(until=D(2026, 12, 31)):
    """Internet 1 000 ₽ on the 10th, utilities 6 000 ₽ on the 25th."""
    result, month = [], D(2026, 10, 1)
    while month <= until:
        result.append(Bill(1, month.replace(day=10), 1000 * RUB, "Интернет"))
        result.append(Bill(2, month.replace(day=25), 6000 * RUB, "Коммуналка"))
        month = (month + timedelta(days=32)).replace(day=1)
    return result


def run(today, incomes=(), **kw):
    values = dict(start=D(2026, 10, 1), today=today, settings=SETTINGS, bills=bills(),
                  incomes=list(incomes))
    values.update(kw)
    return replay(IrregularInput(**values))


def income(id_, day, rub, percent=10, **kw):
    return Income(id_, day, int(round(rub * RUB)), percent, **kw)


REFERENCE = [income(1, D(2026, 10, 3), 20000), income(2, D(2026, 10, 6), 5000)]


def test_reference_scenario():
    r = run(D(2026, 10, 3), REFERENCE[:1])
    split = r.splits[1]
    assert (split.reserve, split.bills, split.free) == (2000 * RUB, 7000 * RUB, 11000 * RUB)
    assert r.stretch_until == D(2026, 10, 16)
    assert r.today_limit == 78571          # 11 000 / 14 = 785,71
    assert r.bills_total == 7000 * RUB and r.reserve == 2000 * RUB

    r = run(D(2026, 10, 4), REFERENCE[:1])
    assert r.today_limit == 84615          # 11 000 / 13 = 846,15

    r = run(D(2026, 10, 6), REFERENCE)
    split = r.splits[2]
    assert (split.reserve, split.bills, split.free) == (500 * RUB, 0, 4500 * RUB)
    assert r.stretch_until == D(2026, 10, 19)
    assert r.today_limit == 110714         # 15 500 / 14 = 1 107,14
    assert r.free_end_today == 15500 * RUB
    assert r.reserve == 2500 * RUB


def test_rounding_and_extreme_percents():
    assert reserve_share(333333, 10) == 33333
    assert reserve_share(5, 10) == 1          # 0,5 копейки → вверх
    assert reserve_share(100000, 7.5) == 7500
    assert split_income(10000, 0) .reserve == 0
    full = split_income(10000, 100, bills_need=5000)
    assert (full.reserve, full.bills, full.free) == (10000, 0, 0)


def test_income_smaller_than_bills_need_warns():
    r = run(D(2026, 10, 3), [income(1, D(2026, 10, 3), 5000)])
    split = r.splits[1]
    assert (split.reserve, split.bills, split.free) == (500 * RUB, 4500 * RUB, 0)
    assert r.shortfall is not None
    assert r.shortfall.amount == 2500 * RUB
    assert r.shortfall.due == D(2026, 10, 25)
    # Nothing went to free money: D is not moved by this income.
    assert r.stretch_until == D(2026, 10, 14)
    assert r.today_limit == 0


def test_two_incomes_on_one_day_and_backdated_income():
    same_day = run(D(2026, 10, 3), [income(1, D(2026, 10, 3), 10000), income(2, D(2026, 10, 3), 10000)])
    assert same_day.splits[1].bills == 7000 * RUB and same_day.splits[2].bills == 0
    assert same_day.free_end_today == 11000 * RUB

    before = run(D(2026, 10, 8), [income(1, D(2026, 10, 3), 20000), income(3, D(2026, 10, 7), 1000)])
    # Income entered later but dated between the two.
    after = run(D(2026, 10, 8), [income(1, D(2026, 10, 3), 20000), income(3, D(2026, 10, 7), 1000),
                                 income(9, D(2026, 10, 5), 3000)])
    assert after.splits[9].free == 2700 * RUB
    assert after.free_end_today == before.free_end_today + 2700 * RUB
    assert after.reserve == before.reserve + 300 * RUB
    assert after.stretch_until == before.stretch_until == D(2026, 10, 20)


def test_overspend_reduces_and_underspend_carries_over():
    base = [income(1, D(2026, 10, 3), 20000)]
    # 3 Oct limit 785,71: spending 1 785,71 leaves 9 214,29 for 13 days.
    over = run(D(2026, 10, 4), base, spent={D(2026, 10, 3): 178571})
    assert over.today_limit == (1100000 - 178571) // 13
    assert over.today_limit < 84615
    under = run(D(2026, 10, 4), base, spent={D(2026, 10, 3): 10000})
    assert under.today_limit == (1100000 - 10000) // 13
    assert under.today_limit > 78571
    today = run(D(2026, 10, 3), base, spent={D(2026, 10, 3): 100000})
    assert today.available_today == 78571 - 100000
    assert today.overspend == 100000 - 78571


def test_window_extends_when_stretch_date_passed():
    r = run(D(2026, 10, 20), [income(1, D(2026, 10, 3), 20000)])
    # D was 16 Oct; on 17 Oct it became 30 Oct and stays there.
    assert r.window_extended is True
    assert r.stretch_until == D(2026, 10, 30)
    assert r.days_left == 11
    assert r.days_without_income == 17
    # Without any income at all: no division by zero.
    empty = run(D(2026, 11, 20), [])
    assert empty.today_limit == 0 and empty.days_left >= 1
    assert empty.window_extended is True


def test_reserve_withdrawals():
    base = [income(1, D(2026, 10, 3), 20000)]
    pay = run(D(2026, 10, 3), base, reserve_moves=[
        ReserveMove(1, D(2026, 10, 3), "withdraw", 50000, "pay_expense", "Врач", 77)])
    assert pay.reserve == 150000 and pay.today_limit == 78571 and pay.available_today == 78571
    to_free = run(D(2026, 10, 3), base, reserve_moves=[
        ReserveMove(1, D(2026, 10, 3), "withdraw", 140000, "to_free", "Нет заказов")])
    assert to_free.today_limit == (1100000 + 140000) // 14
    cover = run(D(2026, 10, 3), base, spent={D(2026, 10, 3): 100000}, reserve_moves=[
        ReserveMove(1, D(2026, 10, 3), "withdraw", 21429, "cover_overspend", "Сломался телефон")])
    assert cover.available_today == 0 and cover.overspend == 0
    assert cover.tomorrow_limit == 78571 * 13 // 13
    negative = run(D(2026, 10, 3), base, reserve_moves=[
        ReserveMove(1, D(2026, 10, 3), "withdraw", 300000, "to_free", "Слишком много")])
    assert negative.reserve_violation == D(2026, 10, 3)


def test_reserve_target_stops_the_percent():
    s = IrregularSettings(reserve_percent=10, reserve_target=2500 * RUB, stretch_days=14, lookahead_days=30)
    r = run(D(2026, 10, 8), [income(1, D(2026, 10, 3), 20000), income(2, D(2026, 10, 6), 10000),
                             income(3, D(2026, 10, 8), 10000)], settings=s)
    assert r.splits[1].reserve == 2000 * RUB
    assert r.splits[2].reserve == 500 * RUB      # up to the target only
    assert r.splits[3].reserve == 0
    assert r.splits[3].free == 10000 * RUB
    assert r.reserve == 2500 * RUB


def test_bill_paid_more_or_less_than_put_aside():
    base = [income(1, D(2026, 10, 3), 20000)]
    paid_more = bills()
    paid_more[0] = Bill(1, D(2026, 10, 10), 1000 * RUB, paid_on=D(2026, 10, 3), actual=1300 * RUB, payment_id=5)
    more = run(D(2026, 10, 3), base, bills=paid_more)
    assert more.spent_today == 300 * RUB
    assert more.bills_total == 6000 * RUB
    assert more.free_end_today == 10700 * RUB

    paid_less = bills()
    paid_less[0] = Bill(1, D(2026, 10, 10), 1000 * RUB, paid_on=D(2026, 10, 3), actual=800 * RUB, payment_id=5)
    less = run(D(2026, 10, 3), base, bills=paid_less)
    assert less.free_end_today == 11200 * RUB
    assert less.today_limit == 1120000 // 14

    to_piggy = bills()
    to_piggy[0] = Bill(1, D(2026, 10, 10), 1000 * RUB, paid_on=D(2026, 10, 3), actual=800 * RUB,
                       to_piggy=200 * RUB, payment_id=5)
    piggy = run(D(2026, 10, 3), base, bills=to_piggy)
    assert piggy.free_end_today == 11000 * RUB


def test_unpaid_bill_after_its_date_stays_put_aside():
    r = run(D(2026, 10, 12), [income(1, D(2026, 10, 3), 20000)])
    assert r.bills_fund[(1, D(2026, 10, 10))] == 1000 * RUB


def test_start_money_goes_to_bills_then_free_without_percent():
    r = replay(IrregularInput(start=D(2026, 10, 1), today=D(2026, 10, 1), start_total=30000 * RUB,
                              start_reserve=5000 * RUB, settings=SETTINGS, bills=bills()))
    assert r.reserve == 5000 * RUB
    assert r.bills_total == 7000 * RUB
    assert r.free_end_today == 18000 * RUB
    assert r.stretch_until == D(2026, 10, 14)
