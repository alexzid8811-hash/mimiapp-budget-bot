"""Dated planning conditions shared by the dashboard and cash-flow forecast."""
from contextlib import contextmanager
from datetime import date, timedelta
import json
from copy import deepcopy

from . import clock
from .budget import Period, add_months, occurrences
from .db import connect
from .money import amount, cents
from .payroll import PayrollConfig, payroll_events_between

PAYROLL_COLUMNS = ('payroll_enabled', 'salary_gross', 'bonus_gross', 'tax_rate', 'salary_day', 'advance_day')
INCOME_COLUMNS = ('id', 'title', 'amount', 'day_of_month', 'kind', 'is_payday', 'active')
BILL_COLUMNS = ('id', 'title', 'amount', 'day_of_month', 'category_id', 'active')


def live_conditions(con, uid):
    settings = con.execute(f"SELECT {','.join(PAYROLL_COLUMNS)} FROM settings WHERE user_id=?", (uid,)).fetchone()
    return {
        'settings': dict(settings),
        'income_rules': [dict(r) for r in con.execute(f"SELECT {','.join(INCOME_COLUMNS)} FROM income_rules WHERE user_id=? AND archived=0", (uid,))],
        'bill_rules': [dict(r) for r in con.execute(f"SELECT {','.join(BILL_COLUMNS)} FROM bill_rules WHERE user_id=? AND archived=0", (uid,))],
    }


@contextmanager
def change_conditions(uid, effective_date=None, *, table=None, record_id=None, payroll=False):
    """Freeze old conditions and commit the edit plus its new version atomically.

    Today's date is the default. Earlier dates must be explicitly selected,
    allowing initial setup/corrections without silently rewriting history.
    """
    effective = effective_date or clock.today()
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        before = live_conditions(con, uid)
        if not con.execute('SELECT 1 FROM plan_history WHERE user_id=? LIMIT 1', (uid,)).fetchone():
            con.execute('INSERT INTO plan_history(user_id,effective_date,snapshot) VALUES(?,?,?)',
                        (uid, '0001-01-01', json.dumps(before)))
        yield con
        after = live_conditions(con, uid)

        def apply_edit(snapshot):
            result = deepcopy(snapshot)
            for key, value in after['settings'].items():
                if payroll or value != before['settings'][key]:
                    result['settings'][key] = value
            for section in ('income_rules', 'bill_rules'):
                old = {r['id']: r for r in before[section]}
                new = {r['id']: r for r in after[section]}
                records = {r['id']: r for r in result[section]}
                for rid in old.keys() | new.keys():
                    if old.get(rid) != new.get(rid) or (section == table and rid == record_id):
                        if rid in new:
                            records[rid] = new[rid]
                        else:
                            records.pop(rid, None)
                result[section] = list(records.values())
            return result

        # Correct only the edited fields/rules. A backdated salary correction
        # must not move unrelated bill changes into earlier days.
        original = con.execute('SELECT snapshot FROM plan_history WHERE user_id=? AND effective_date<=? ORDER BY effective_date DESC LIMIT 1',
                               (uid, effective.isoformat())).fetchone()
        later = list(con.execute('SELECT id,snapshot FROM plan_history WHERE user_id=? AND effective_date>?', (uid, effective.isoformat())))
        con.execute('INSERT INTO plan_history(user_id,effective_date,snapshot) VALUES(?,?,?) ON CONFLICT(user_id,effective_date) DO UPDATE SET snapshot=excluded.snapshot',
                    (uid, effective.isoformat(), json.dumps(apply_edit(json.loads(original['snapshot'])))))
        for row in later:
            con.execute('UPDATE plan_history SET snapshot=? WHERE id=?', (json.dumps(apply_edit(json.loads(row['snapshot']))), row['id']))
        con.execute("DELETE FROM reserve_movements WHERE user_id=? AND source='auto' AND period_start>=?",
                    (uid, effective.replace(day=1).isoformat()))
        # A backdated correction deliberately recalculates card periods funded
        # from that day on; earlier closed periods keep their daily limits.
        con.execute("DELETE FROM card_allocations WHERE user_id=? AND funded_on>=?",
                    (uid, effective.isoformat()))


def condition_segments(uid, start, end):
    if start > end:
        return []
    with connect() as con:
        history = [dict(r) for r in con.execute(
            'SELECT effective_date,snapshot FROM plan_history WHERE user_id=? AND effective_date<=? ORDER BY effective_date',
            (uid, end.isoformat()))]
        live = live_conditions(con, uid)
    if not history:
        return [(start, end, live)]
    result = []
    for i, row in enumerate(history):
        left = max(start, date.fromisoformat(row['effective_date']))
        right = end if i + 1 == len(history) else min(end, date.fromisoformat(history[i+1]['effective_date']) - timedelta(days=1))
        if left <= right:
            result.append((left, right, json.loads(row['snapshot'])))
    return result


def payroll_config_for_accrual_month(base: PayrollConfig, changes: list[dict], year: int, month: int) -> PayrollConfig:
    """Apply the latest planned salary and bonus change to an accrual month."""
    accrual_month = date(year, month, 1)
    applicable = [
        change for change in changes
        if date.fromisoformat(change['effective_month']) <= accrual_month
    ]
    if not applicable:
        return base
    change = applicable[-1]
    return PayrollConfig(
        salary_gross=float(change['salary_gross']),
        bonus_gross=float(change['bonus_gross']),
        tax_rate=base.tax_rate,
        salary_day=base.salary_day,
        advance_day=base.advance_day,
    )


def income_events(uid, start, end, payroll_changes_through: date | None = None):
    with connect() as con:
        vacations = [dict(r) for r in con.execute('SELECT * FROM vacations WHERE user_id=?', (uid,))]
        payroll_changes = [
            dict(r) for r in con.execute(
                "SELECT effective_month,salary_gross,bonus_gross FROM payroll_changes "
                "WHERE user_id=? ORDER BY effective_month",
                (uid,),
            )
        ]
    if payroll_changes_through is not None:
        payroll_changes = [
            change for change in payroll_changes
            if date.fromisoformat(change['effective_month']) <= payroll_changes_through
        ]
    events = []
    for left, right, conditions in condition_segments(uid, start, end):
        cfg = conditions['settings']
        enabled = bool(cfg['payroll_enabled'])
        if enabled:
            config = PayrollConfig(**{k: cfg[k] for k in PAYROLL_COLUMNS if k != 'payroll_enabled'})
            config_for_month = lambda year, month, base=config: payroll_config_for_accrual_month(
                base, payroll_changes, year, month
            )
            for event in payroll_events_between(left, right, config_for_month, vacations):
                events.append({**event, 'is_payday': event['kind'] in {'salary', 'advance'}})
        else:
            for vacation in vacations:
                day = date.fromisoformat(vacation['payment_date'])
                if left <= day <= right:
                    events.append({'date': day, 'amount': vacation['amount'], 'title': 'Отпускные', 'is_payday': False})
        payday_rules = []
        for rule in conditions['income_rules']:
            if not rule['active'] or (enabled and rule['kind'] != 'other'):
                continue
            is_payday = not enabled and (bool(rule['is_payday']) or rule['kind'] in {'salary', 'advance'})
            if is_payday:
                payday_rules.append(rule)
            for day in occurrences([rule['day_of_month']], left, right, move_to_previous_workday=is_payday):
                events.append({'date': day, 'amount': rule['amount'], 'title': rule['title'], 'is_payday': is_payday})
        if not enabled and not payday_rules:
            for day in occurrences([7, 22], left, right, move_to_previous_workday=True):
                events.append({'date': day, 'amount': 0, 'title': 'выплата', 'is_payday': True})
    return sorted(events, key=lambda e: e['date'])


def income_map(uid, start, end, include_manual=True, payroll_changes_through: date | None = None):
    """Income by its real receipt date, used for history and account balance."""
    totals = {}
    for event in income_events(uid, start, end, payroll_changes_through):
        day = event['date']
        totals[day] = totals.get(day, 0) + cents(event['amount'])
    if include_manual:
        with connect() as con:
            for row in con.execute("SELECT tx_date,amount FROM transactions WHERE user_id=? AND type='income' AND tx_date BETWEEN ? AND ?", (uid, start.isoformat(), end.isoformat())):
                day = date.fromisoformat(row['tx_date'])
                totals[day] = totals.get(day, 0) + cents(row['amount'])
    return {day: amount(value) for day, value in sorted(totals.items())}


def budget_income_events(uid, start, end, payroll_changes_through: date | None = None):
    """Scheduled income on the day it becomes available to the daily budget.

    Salary and advance are available only from the calendar day after their
    actual, already workday-adjusted payment date. Other income keeps its
    own date.
    """
    result = []
    for event in income_events(
        uid, start - timedelta(days=1), end, payroll_changes_through
    ):
        budget_date = event['date'] + timedelta(days=1) if event['is_payday'] else event['date']
        if start <= budget_date <= end:
            result.append({**event, 'budget_date': budget_date})
    return sorted(result, key=lambda event: event['budget_date'])


def budget_income_map(uid, start, end, include_manual=True, payroll_changes_through: date | None = None):
    """Income schedule used by daily-budget and cash-flow calculations."""
    totals = {}
    for event in budget_income_events(uid, start, end, payroll_changes_through):
        day = event['budget_date']
        totals[day] = totals.get(day, 0) + cents(event['amount'])
    if include_manual:
        with connect() as con:
            for row in con.execute("SELECT tx_date,amount FROM transactions WHERE user_id=? AND type='income' AND tx_date BETWEEN ? AND ?", (uid, start.isoformat(), end.isoformat())):
                day = date.fromisoformat(row['tx_date'])
                totals[day] = totals.get(day, 0) + cents(row['amount'])
    return {day: amount(value) for day, value in sorted(totals.items())}


def bill_events(uid, start, end):
    events = {}
    for left, right, conditions in condition_segments(uid, start, end):
        for rule in conditions['bill_rules']:
            if rule['active']:
                for day in occurrences([rule['day_of_month']], left, right):
                    events[(rule['id'], day.isoformat())] = {
                        **rule,
                        'due_date': day.isoformat(),
                        'planned_amount': rule['amount'],
                        'paid': False,
                        'payment_id': None,
                        'remainder_destination': 'budget',
                        'remainder_amount': 0,
                    }
    with connect() as con:
        for row in con.execute(
            "SELECT t.bill_rule_id,t.bill_due_date,t.id AS payment_id,t.amount,t.note,t.category_id,"
            "t.bill_planned_amount,p.amount AS remainder_amount "
            "FROM transactions t LEFT JOIN piggy_bank_movements p "
            "ON p.user_id=t.user_id AND p.bill_payment_id=t.id "
            "WHERE t.user_id=? AND t.bill_rule_id IS NOT NULL AND t.bill_due_date BETWEEN ? AND ?",
            (uid, start.isoformat(), end.isoformat())):
            key = (row['bill_rule_id'], row['bill_due_date'])
            event = events.setdefault(key, {
                'id': row['bill_rule_id'], 'title': row['note'], 'category_id': row['category_id'],
                'due_date': row['bill_due_date'], 'planned_amount': row['bill_planned_amount'] or row['amount'],
            })
            event.update(
                amount=row['amount'],
                planned_amount=row['bill_planned_amount'] or event.get('planned_amount') or row['amount'],
                paid=True,
                payment_id=row['payment_id'],
                remainder_destination='piggy' if row['remainder_amount'] is not None else 'budget',
                remainder_amount=row['remainder_amount'] or 0,
            )
    # A monthly bill is due once a month. Moving its day mid-month leaves an
    # occurrence in both the old and the new version of the plan: the paid one
    # wins, otherwise the date from the newer version (the later one).
    by_month = {}
    for event in events.values():
        by_month.setdefault((event['id'], event['due_date'][:7]), []).append(event)
    result = []
    for group in by_month.values():
        paid = [e for e in group if e.get('paid')]
        result.extend(paid or [max(group, key=lambda e: e['due_date'])])
    return sorted(result, key=lambda e: (e['due_date'], e['id']))


def mandatory_map(uid, start, end):
    totals = {}
    for event in bill_events(uid, start, end):
        day = date.fromisoformat(event['due_date'])
        totals[day] = totals.get(day, 0) + cents(event['amount'])
    return {day: amount(value) for day, value in totals.items()}


def unpaid_mandatory_map(uid, start, end):
    """Planned obligations that have not already left the account.

    Paid bill transactions are real cash movements and are accounted for by
    their transaction date.  Keeping them in the forecast as well would spend
    the same money twice, especially when a future bill is paid early.
    """
    totals = {}
    for event in bill_events(uid, start, end):
        if event.get('paid'):
            continue
        day = date.fromisoformat(event['due_date'])
        totals[day] = totals.get(day, 0) + cents(event['amount'])
    return {day: amount(value) for day, value in totals.items()}


def bill_planned_amount(uid, bill_id, due_date):
    """Return the historical planned amount for one bill occurrence."""
    for left, right, conditions in condition_segments(uid, due_date, due_date):
        for rule in conditions['bill_rules']:
            if rule['id'] != bill_id or not rule['active']:
                continue
            if due_date in occurrences([rule['day_of_month']], left, right):
                return amount(cents(rule['amount']))
    return None


def payday_boundaries(uid, start, end):
    """Budget-period starts, one day after each actual payday."""
    dates = {
        event['budget_date']: event['title']
        for event in budget_income_events(uid, start, end)
        if event['is_payday']
    }
    return [{'date': day, 'kind': title} for day, title in sorted(dates.items())]


def user_period(uid, as_of):
    dates = [e['date'] for e in payday_boundaries(uid, add_months(as_of, -3), add_months(as_of, 3))]
    return Period(max(d for d in dates if d <= as_of), min(d for d in dates if d > as_of) - timedelta(days=1))


def next_periods(uid, after, count):
    dates = [e['date'] for e in payday_boundaries(uid, after, add_months(after, count + 2))]
    return [Period(a, b - timedelta(days=1)) for a, b in zip(dates, dates[1:])][:count]
