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
