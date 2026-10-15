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
    created: str = ""             # when it was added (UTC timestamp, like created_at)

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
    created: str | None = None    # when it was recorded (UTC timestamp)


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


# ---------------------------------------------------------------------------
# Envelopes on accounts
# ---------------------------------------------------------------------------
#
# Every envelope of the budget (spending money, buffer, piggy bank, reserve)
# lies on one non-credit account; several envelopes may share an account.
# The budget moves money between envelopes on its own (a payday fills the
# buffer, the buffer funds the card...).  When the two envelopes lie on
# different accounts, the money has to be moved by hand: these are the
# transfer hints («Нужно перевести»).
#
# A budget operation is described by what it does to the envelopes and what it
# does to the accounts.  On every account the difference between the two is
# money that lies on the wrong account; accounts with too much send it to the
# accounts with too little.  Summed per pair of accounts since the pair's
# window (the later opening date and the latest assignment of an envelope on
# either account), minus the plain transfers made between them, that is the
# hint.  A transfer never creates a hint on its own: it only pays one off.
#
# On the first day of a window the time matters: the opening balance (or the
# amount moved with an envelope) already holds what happened earlier that day.
# An operation of that day counts only when it was recorded later; the
# budget's own movements (no record, ``created`` is None) do not count.


@dataclass(frozen=True)
class BudgetOp:
    day: date
    envelopes: dict          # envelope -> signed change of its amount
    physical: dict           # account id -> signed change of its money
    created: str | None = None   # when it was recorded; None: the budget's own movement


@dataclass(frozen=True)
class Hint:
    from_id: int
    to_id: int
    amount: int


@dataclass(frozen=True)
class AccountBreakdown:
    account_id: int
    envelopes: dict          # envelope -> amount, in the order of the mode
    envelopes_total: int
    outgoing_hints: int      # still to be sent from this account
    difference: int          # balance - envelopes - outgoing hints


def _legs(op: BudgetOp, placement: dict, credit: set[int]) -> list[tuple[int, int, int]]:
    mismatch: dict[int, int] = {}
    for account_id, value in op.physical.items():
        if account_id not in credit and value:
            mismatch[account_id] = mismatch.get(account_id, 0) + value
    for envelope, value in op.envelopes.items():
        account_id = placement.get(envelope)
        if account_id is not None and value:
            mismatch[account_id] = mismatch.get(account_id, 0) - value
    senders = sorted([a, v] for a, v in mismatch.items() if v > 0)
    receivers = sorted([a, -v] for a, v in mismatch.items() if v < 0)
    legs = []
    for sender in senders:
        for receiver in receivers:
            take = min(sender[1], receiver[1])
            if take > 0:
                legs.append((sender[0], receiver[0], take))
                sender[1] -= take
                receiver[1] -= take
    return legs


def pair_window(a: Account, b: Account, placement: dict, assigned_on: dict) -> tuple[date, str]:
    """Since when the budget's movements between two accounts are counted:
    the later opening and the latest envelope assignment on either of them.
    ``assigned_on`` maps an envelope to (day, UTC timestamp)."""
    moments = [(a.opening_date, a.created), (b.opening_date, b.created)]
    moments += [assigned_on[e] for e, acc in placement.items() if acc in (a.id, b.id) and e in assigned_on]
    return max(moments)


def _counts_since(day: date, created: str | None, window: tuple[date, str]) -> bool:
    if day != window[0]:
        return day > window[0]
    return created is not None and created >= window[1]


def transfer_hints(
    accounts: list[Account], placement: dict, assigned_on: dict, ops: list[BudgetOp], transfers: list[Transfer],
) -> list[Hint]:
    """Transfers the budget needs between accounts that nobody has made yet.
    ``transfers`` are the plain ones (made by hand or with «Перевёл»)."""
    by_id = {a.id: a for a in accounts}
    credit = {a.id for a in accounts if a.is_credit}
    windows: dict[tuple[int, int], tuple[date, str]] = {}

    def window(x: int, y: int) -> tuple[date, str]:
        key = (min(x, y), max(x, y))
        if key not in windows:
            windows[key] = pair_window(by_id[key[0]], by_id[key[1]], placement, assigned_on)
        return windows[key]

    needed: dict[tuple[int, int], int] = {}
    for op in ops:
        for sender, receiver, value in _legs(op, placement, credit):
            if sender not in by_id or receiver not in by_id:
                continue
            if not _counts_since(op.day, op.created, window(sender, receiver)):
                continue
            key = (min(sender, receiver), max(sender, receiver))
            needed[key] = needed.get(key, 0) + (value if sender == key[0] else -value)
    paid: dict[tuple[int, int], int] = {}
    for t in transfers:
        if t.to_id is None or t.from_id not in by_id or t.to_id not in by_id:
            continue
        key = (min(t.from_id, t.to_id), max(t.from_id, t.to_id))
        if key in needed and _counts_since(t.day, t.created or "", window(*key)):
            paid[key] = paid.get(key, 0) + (t.amount if t.from_id == key[0] else -t.amount)
    for key, value in needed.items():
        rest = value - paid.get(key, 0)
        # A transfer only pays a need off; going further never turns it around.
        needed[key] = max(0, rest) if value > 0 else min(0, rest) if value < 0 else 0
    hints = []
    for (low, high), value in sorted(needed.items()):
        if value > 0:
            hints.append(Hint(low, high, value))
        elif value < 0:
            hints.append(Hint(high, low, -value))
    return hints


def breakdown(
    states: dict[int, AccountState], placement: dict, amounts: dict, hints: list[Hint], order: tuple,
) -> dict[int, AccountBreakdown]:
    """How the money of every non-credit account divides between the
    envelopes lying on it, and what is left over (positive: outside the
    budget) or missing (negative)."""
    result = {}
    for account_id, state in states.items():
        if state.kind == "credit":
            continue
        envelopes = {e: amounts.get(e, 0) for e in order if placement.get(e) == account_id}
        total = sum(envelopes.values())
        outgoing = sum(h.amount for h in hints if h.from_id == account_id)
        result[account_id] = AccountBreakdown(
            account_id, envelopes, total, outgoing, state.balance - total - outgoing,
        )
    return result
