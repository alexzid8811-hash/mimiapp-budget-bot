"""Cards and accounts: where the money physically lies.

All amounts are integer kopecks.  The module has no database access, so every
rule can be tested directly (like :mod:`app.ledger`).

The budget (envelopes: card, buffer, piggy bank, reserve) says what the money
is meant for; accounts say where it is.  Nothing here changes the budget.

Balances
--------
* A balance is never stored.  It is the opening balance plus every operation
  dated on or after the account's opening date; earlier operations (and
  operations without an account) do not touch it.
* Debit cards, savings accounts and cash:
  ``balance = opening + incomes + transfers in - expenses - transfers out
  + adjustments``.
* A credit card keeps a debt instead:
  ``debt = opening + expenses + transfers out - (transfers in - interest)
  - adjustments`` and ``available = limit - debt``.  Internally a credit card
  is a balance of ``-debt``, so both kinds share one formula; the interest of a
  repayment is the part of it that does not reduce the debt.
* "Всего" is the sum of the balances of every non-credit account, archived ones
  included (normally zero, but an honest total counts them).  Credit cards are
  never part of it: neither the limit nor the debt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


KINDS = ("debit", "savings", "credit", "cash")


@dataclass(frozen=True)
class Account:
    id: int
    kind: str                     # debit | savings | credit | cash
    opening_balance: int          # for a credit card: the opening debt
    opening_date: date
    credit_limit: int | None = None
    archived: bool = False

    @property
    def is_credit(self) -> bool:
        return self.kind == "credit"


@dataclass(frozen=True)
class Flow:
    """Money arriving on (positive) or leaving (negative) one account:
    an expense, an income or an adjustment."""
    account_id: int
    day: date
    amount: int


@dataclass(frozen=True)
class Transfer:
    from_id: int
    to_id: int | None             # None: the money left the user's accounts
    day: date
    amount: int
    interest: int = 0             # repayment of a credit card: bank interest in it


@dataclass
class AccountsInput:
    accounts: list[Account]
    flows: list[Flow] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)


@dataclass(frozen=True)
class AccountState:
    id: int
    kind: str
    balance: int                  # money on the account; -debt for a credit card
    archived: bool
    credit_limit: int | None = None

    @property
    def debt(self) -> int:
        return -self.balance if self.kind == "credit" else 0

    @property
    def available(self) -> int | None:
        if self.kind != "credit" or self.credit_limit is None:
            return None
        return self.credit_limit - self.debt


@dataclass
class AccountsResult:
    states: dict[int, AccountState]
    total: int                    # «Всего»: non-credit accounts, archived included
    credit_debt: int              # sum of the credit cards' debts


def _counts(account: Account | None, day: date) -> bool:
    return account is not None and day >= account.opening_date


def balances(inp: AccountsInput) -> dict[int, int]:
    """Balance of every account (``-debt`` for a credit card)."""
    by_id = {a.id: a for a in inp.accounts}
    result = {a.id: (-a.opening_balance if a.is_credit else a.opening_balance) for a in inp.accounts}
    for flow in inp.flows:
        if _counts(by_id.get(flow.account_id), flow.day):
            result[flow.account_id] += flow.amount
    for transfer in inp.transfers:
        if _counts(by_id.get(transfer.from_id), transfer.day):
            result[transfer.from_id] -= transfer.amount
        target = by_id.get(transfer.to_id) if transfer.to_id is not None else None
        if _counts(target, transfer.day):
            interest = transfer.interest if target.is_credit else 0
            result[target.id] += transfer.amount - interest
    return result


def calculate(inp: AccountsInput) -> AccountsResult:
    values = balances(inp)
    states = {
        a.id: AccountState(a.id, a.kind, values[a.id], a.archived, a.credit_limit)
        for a in inp.accounts
    }
    total = sum(s.balance for s in states.values() if s.kind != "credit")
    debt = sum(s.debt for s in states.values() if s.kind == "credit")
    return AccountsResult(states=states, total=total, credit_debt=debt)


def opening_from_target(account: Account, target: int, current: int) -> int:
    """Adjustment that brings ``current`` (a balance, ``-debt`` for a credit
    card) to the real ``target`` the user entered (a debt for a credit card)."""
    wanted = -target if account.is_credit else target
    return wanted - current
