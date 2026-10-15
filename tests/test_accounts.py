"""Rules of app/accounts.py: balances, credit card debt, «Всего»."""
from datetime import date

from app.accounts import Account, AccountsInput, Flow, Transfer, calculate, opening_from_target

D0 = date(2026, 10, 1)
RUB = 100


def card(id, balance, kind="debit", limit=None, opened=D0, archived=False):
    return Account(id, kind, balance * RUB, opened, limit * RUB if limit is not None else None, archived)


def expense(account, value, day=D0):
    return Flow(account, day, -value * RUB)


def income(account, value, day=D0):
    return Flow(account, day, value * RUB)


def transfer(a, b, value, day=D0, interest=0):
    return Transfer(a, b, day, value * RUB, interest * RUB)


def rub(result, account_id):
    return result.states[account_id].balance / RUB


def test_debit_balance_counts_every_operation_kind():
    inp = AccountsInput(
        accounts=[card(1, 1000), card(2, 0)],
        flows=[income(1, 500), expense(1, 200), Flow(1, D0, -50 * RUB)],  # adjustment -50
        transfers=[transfer(1, 2, 300), transfer(2, 1, 100)],
    )
    result = calculate(inp)
    assert rub(result, 1) == 1000 + 500 - 200 - 50 - 300 + 100
    assert rub(result, 2) == 300 - 100
    assert result.total == (1050 + 200) * RUB


def test_operations_before_the_opening_date_do_not_count():
    later = date(2026, 10, 5)
    inp = AccountsInput(
        accounts=[card(1, 1000, opened=later), card(2, 0)],
        flows=[expense(1, 100, D0), expense(1, 10, later)],
        transfers=[transfer(2, 1, 50, D0)],
    )
    result = calculate(inp)
    assert rub(result, 1) == 990
    # The other side of the transfer still counts: card 2 was open.
    assert rub(result, 2) == -50


def test_credit_card_keeps_a_debt_outside_the_total():
    inp = AccountsInput(
        accounts=[card(1, 1000), card(9, 200, kind="credit", limit=5000)],
        flows=[expense(9, 300)],
        transfers=[transfer(1, 9, 100)],
    )
    result = calculate(inp)
    state = result.states[9]
    assert state.debt == (200 + 300 - 100) * RUB
    assert state.available == (5000 - 400) * RUB
    assert result.total == 900 * RUB
    assert result.credit_debt == 400 * RUB


def test_interest_of_a_repayment_does_not_reduce_the_debt():
    inp = AccountsInput(
        accounts=[card(1, 10000), card(9, 0, kind="credit", limit=50000)],
        flows=[expense(9, 3000)],
        transfers=[transfer(1, 9, 3000, interest=500)],
    )
    result = calculate(inp)
    assert result.states[9].debt == 500 * RUB
    assert result.states[9].available == 49500 * RUB
    assert rub(result, 1) == 7000


def test_credit_adjustments_interest_and_cashback():
    # Interest: the debt grows (money position falls); cashback: it shrinks.
    inp = AccountsInput(
        accounts=[card(9, 1000, kind="credit", limit=5000)],
        flows=[Flow(9, D0, -70 * RUB), Flow(9, D0, 20 * RUB)],
    )
    assert calculate(inp).states[9].debt == 1050 * RUB


def test_archived_accounts_stay_in_the_total_and_money_can_leave():
    inp = AccountsInput(
        accounts=[card(1, 100), card(2, 30, archived=True)],
        transfers=[Transfer(1, None, D0, 40 * RUB)],
    )
    result = calculate(inp)
    assert result.total == (60 + 30) * RUB


def test_opening_from_target():
    debit, credit = card(1, 0), card(9, 0, kind="credit")
    assert opening_from_target(debit, 1500, 1000) == 500
    # A credit card: the user enters the real debt 500 while the app has 300.
    assert opening_from_target(credit, 500, -300) == -200


def test_example_1_balances_total_and_credit_card():
    """Example 1 of the task, in terms of accounts only."""
    accounts = [card(1, 40000), card(2, 15000), card(3, 100000, kind="savings"),
                card(9, 0, kind="credit", limit=50000)]
    flows, transfers = [], []

    def state():
        return calculate(AccountsInput(accounts, list(flows), list(transfers)))

    assert state().total == 155000 * RUB
    flows.append(expense(1, 2000))
    assert rub(state(), 1) == 38000 and state().total == 153000 * RUB
    flows.append(expense(9, 3000))
    r = state()
    assert r.total == 153000 * RUB and r.states[9].debt == 3000 * RUB
    assert r.states[9].available == 47000 * RUB and r.credit_debt == 3000 * RUB
    transfers.append(transfer(1, 2, 10000))
    r = state()
    assert (rub(r, 1), rub(r, 2), r.total) == (28000, 25000, 153000 * RUB)
    transfers.append(transfer(1, 9, 3000))
    r = state()
    assert (rub(r, 1), r.total, r.states[9].debt, r.states[9].available) == (25000, 150000 * RUB, 0, 50000 * RUB)
    flows.append(income(1, 60000))
    r = state()
    assert (rub(r, 1), r.total) == (85000, 210000 * RUB)


# ---------- Envelopes on accounts: breakdown and transfer hints ----------

from app.accounts import BudgetOp, breakdown, transfer_hints  # noqa: E402

PAYROLL = ("spending", "buffer", "piggy")
IRREGULAR = ("spending", "buffer", "piggy", "reserve")
CREATED = "2026-10-01 10:00:00"


def acc(id, balance, kind="debit"):
    return Account(id, kind, balance * RUB, D0, None, False, CREATED)


def kop(values):
    return {k: v * RUB for k, v in values.items()}


def split_of(accounts, flows, transfers, placement, amounts, hints=(), order=PAYROLL):
    result = calculate(AccountsInput(accounts, flows, transfers))
    parts = breakdown(result.states, placement, kop(amounts), list(hints), order)
    return result, {a: (p.envelopes_total / RUB, p.difference / RUB) for a, p in parts.items()}


def test_example_2_cards_split_between_envelopes():
    """Example 2: card 1 (main) holds spending and buffer, card 2 the piggy bank."""
    accounts = [acc(1, 50000), acc(2, 80000), acc(9, 0, "credit")]
    placement = {"spending": 1, "buffer": 1, "piggy": 2}
    flows, transfers = [], []
    env = {"spending": 20000, "buffer": 30000, "piggy": 80000}

    def check(expected):
        result, parts = split_of(accounts, flows, transfers, placement, env)
        assert {a: rub(result, a) for a in (1, 2)} == expected
        assert parts[1][1] == 0 and parts[2][1] == 0
        return result, parts

    check({1: 50000, 2: 80000})
    flows.append(expense(1, 1500)); env["spending"] -= 1500                          # 1
    check({1: 48500, 2: 80000})
    flows.append(expense(9, 3000))                                                     # 2
    check({1: 48500, 2: 80000})
    transfers.append(transfer(1, 2, 500)); env["spending"] -= 500; env["piggy"] += 500  # 3
    result, parts = check({1: 48000, 2: 80500})
    assert result.total == 128500 * RUB
    flows.append(income(1, 60000)); env["buffer"] += 60000 - 35000; env["spending"] += 35000  # 4, 5
    result, _ = check({1: 108000, 2: 80500})
    assert result.total == 188500 * RUB
    transfers.append(transfer(1, 9, 3000)); env["spending"] -= 3000                     # 6
    result, _ = check({1: 105000, 2: 80500})
    assert result.states[9].debt == 0 and result.total == 185500 * RUB
    flows.append(expense(2, 2000)); env["piggy"] -= 2000                                # 7
    result, _ = check({1: 105000, 2: 78500})
    assert result.total == 183500 * RUB
    flows.append(expense(1, 800)); transfers.append(transfer(1, 2, 200))                 # 8
    env["buffer"] -= 1000; env["piggy"] += 200
    result, parts = check({1: 104000, 2: 78700})
    assert (env["spending"], env["buffer"]) == (50000, 54000) and result.total == 182700 * RUB
    flows.append(Flow(2, D0, 500 * RUB))                                                # 9: interest
    _, parts = split_of(accounts, flows, transfers, placement, env)
    assert parts[2][1] == 500                                                           # «вне бюджета»
    env["piggy"] += 500                                                                 # «Добавить в копилку»
    check({1: 104000, 2: 79200})
    env["buffer"] += 25000                                                              # 10: auto-counted
    _, parts = split_of(accounts, flows, transfers, placement, env)
    assert parts[1][1] == -25000                                                        # «не хватает»
    flows.append(income(1, 25000))                                                      # «Записать приход»
    check({1: 129000, 2: 79200})


def test_example_3_payday_hint_between_buffer_and_spending_cards():
    accounts = [acc(1, 30000), acc(3, 25000)]
    placement = {"buffer": 1, "spending": 3, "piggy": 1}
    payday = date(2026, 10, 7)
    ops = [BudgetOp(payday, {"buffer": -15000 * RUB, "spending": 15000 * RUB}, {})]
    hints = transfer_hints(accounts, placement, {}, ops, [])
    assert [(h.from_id, h.to_id, h.amount / RUB) for h in hints] == [(1, 3, 15000)]
    env = {"buffer": 15000, "spending": 40000, "piggy": 0}
    _, parts = split_of(accounts, [], [], placement, env, hints)
    assert parts[3][1] == -15000          # «не хватает 15 000»
    assert parts[1][1] == 0               # no «вне бюджета» on the source
    done = [transfer(1, 3, 15000, payday)]
    assert transfer_hints(accounts, placement, {}, ops, done) == []
    _, parts = split_of(accounts, [], done, placement, env)
    assert parts[3][1] == 0 and parts[1][1] == 0


def test_example_4_irregular_income_split_between_two_cards():
    accounts = [acc(1, 18000), acc(2, 70000)]
    placement = {"spending": 1, "buffer": 1, "piggy": 2, "reserve": 2}
    env = {"spending": 10000, "buffer": 8000, "piggy": 50000, "reserve": 20000}
    flows, transfers = [], []
    day = date(2026, 10, 5)
    op = BudgetOp(day, kop({"reserve": 2000, "buffer": 3000, "spending": 15000}), {1: 20000 * RUB}, "2026-10-05 09:00:00")
    flows.append(income(1, 20000, day))
    env.update(reserve=22000, buffer=11000, spending=25000)
    hints = transfer_hints(accounts, placement, {}, [op], [])
    assert [(h.from_id, h.to_id, h.amount / RUB) for h in hints] == [(1, 2, 2000)]
    result, parts = split_of(accounts, flows, transfers, placement, env, hints, IRREGULAR)
    assert rub(result, 1) == 38000 and parts[2][1] == -2000 and parts[1][1] == 0
    transfers.append(Transfer(1, 2, day, 2000 * RUB, created="2026-10-05 09:01:00"))      # 2: «Перевёл»
    assert transfer_hints(accounts, placement, {}, [op], transfers) == []
    result, parts = split_of(accounts, flows, transfers, placement, env, (), IRREGULAR)
    assert (rub(result, 1), rub(result, 2)) == (36000, 72000) and parts[1][1] == parts[2][1] == 0
    transfers.append(transfer(2, 1, 4000)); env.update(reserve=18000, spending=29000)     # 3: linked
    result, parts = split_of(accounts, flows, transfers, placement, env, (), IRREGULAR)
    assert (rub(result, 1), rub(result, 2)) == (40000, 68000) and parts[1][1] == parts[2][1] == 0
    flows.append(expense(2, 5000)); env["reserve"] -= 5000                                # 4: from reserve
    transfers.append(transfer(1, 2, 400)); env.update(spending=28600, piggy=50400)        # 5: linked
    result, parts = split_of(accounts, flows, transfers, placement, env, (), IRREGULAR)
    assert (rub(result, 1), rub(result, 2)) == (39600, 63400) and parts[1][1] == parts[2][1] == 0


def test_same_card_envelopes_need_no_transfer():
    accounts = [acc(1, 100)]
    ops = [BudgetOp(date(2026, 10, 7), {"buffer": -50 * RUB, "spending": 50 * RUB}, {})]
    assert transfer_hints(accounts, {"buffer": 1, "spending": 1}, {}, ops, []) == []


def test_a_transfer_never_creates_a_hint_on_its_own():
    accounts = [acc(1, 100), acc(2, 100)]
    placement = {"spending": 1, "buffer": 1, "piggy": 1}
    assert transfer_hints(accounts, placement, {}, [], [transfer(1, 2, 50)]) == []
    # Paying more than needed does not turn the hint around either.
    ops = [BudgetOp(date(2026, 10, 7), {"spending": -30 * RUB}, {2: -30 * RUB}, CREATED)]
    assert [(h.from_id, h.to_id, h.amount) for h in transfer_hints(accounts, placement, {}, ops, [])] == [(1, 2, 3000)]
    assert transfer_hints(accounts, placement, {}, ops, [transfer(1, 2, 50, date(2026, 10, 8))]) == []


def test_window_starts_at_the_latest_assignment():
    accounts = [acc(1, 100), acc(3, 0)]
    placement = {"buffer": 1, "spending": 3}
    moved = date(2026, 10, 10)
    assigned = {"spending": (moved, "2026-10-10 12:00:00")}
    ops = [
        BudgetOp(date(2026, 10, 7), {"buffer": -10 * RUB, "spending": 10 * RUB}, {}),       # before the move
        BudgetOp(moved, {"buffer": -20 * RUB, "spending": 20 * RUB}, {}),                   # in the opening balance
        BudgetOp(moved, {"spending": -5 * RUB}, {1: -5 * RUB}, "2026-10-10 11:00:00"),      # recorded earlier that day
        BudgetOp(moved, {"spending": -7 * RUB}, {1: -7 * RUB}, "2026-10-10 13:00:00"),      # recorded after
        BudgetOp(date(2026, 10, 22), {"buffer": -40 * RUB, "spending": 40 * RUB}, {}),
    ]
    hints = transfer_hints(accounts, placement, assigned, ops, [])
    assert [(h.from_id, h.to_id, h.amount / RUB) for h in hints] == [(1, 3, 33)]


def test_credit_cards_take_no_part_in_hints():
    accounts = [acc(1, 100), acc(9, 0, "credit")]
    ops = [BudgetOp(date(2026, 10, 7), {"spending": -30 * RUB}, {1: -30 * RUB, 9: 27 * RUB}, CREATED)]
    assert transfer_hints(accounts, {"spending": 1}, {}, ops, []) == []
