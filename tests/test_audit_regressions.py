import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import clock, planning
from app.auth import current_user
from app.backup import export_user_data, restore_user_data
from app.cashflow import calculate_cashflow_plan
from app.cashflow_app import app, cashflow_period_rows, cashflow_snapshot, add_piggy_bank_movement, PiggyBankMovementIn, piggy_bank_balance
from app.db import connect, ensure_user, init_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'budget.sqlite3'))
    monkeypatch.setenv('DEV_MODE', 'true')
    monkeypatch.setattr(clock, 'today', lambda: date(2026, 9, 15))
    monkeypatch.setattr('app.russian_calendar._remote_year', lambda _: None)
    init_db()
    ensure_user(1)
    with connect() as con:
        con.execute("UPDATE income_rules SET amount=10000 WHERE user_id=1 AND kind='salary'")
        con.execute("UPDATE settings SET cashflow_enabled=1,cashflow_start_date='2026-09-01',"
                    "initial_reserve=1000,cashflow_start_capital=1000,forecast_months=1 WHERE user_id=1")
    with TestClient(app) as value:
        yield value


def salary_rule():
    with connect() as con:
        return dict(con.execute("SELECT * FROM income_rules WHERE user_id=1 AND kind='salary'").fetchone())


def test_unreceived_planned_income_does_not_inflate_start_capital(client):
    # The salary scheduled for September 7 is still only a forecast because no
    # matching actual income transaction was recorded.
    assert cashflow_snapshot(1)["current_cash"] == 1000

    response = client.post(
        "/api/transactions",
        json={
            "type": "income",
            "amount": 10300,
            "tx_date": "2026-09-11",
            "category_id": None,
            "note": "Фактически получено",
        },
    )
    assert response.status_code == 200
    assert cashflow_snapshot(1)["current_cash"] == 11300


@pytest.mark.parametrize('spent,expected_daily,available,shortfall', [(900, 11.11, -800, 0), (1100, 0, -1000, 100)])
def test_overspend_uses_actual_cash_without_spending_buffer(spent, expected_daily, available, shortfall):
    plan = calculate_cashflow_plan(today=date(2026, 9, 15), horizon_end=date(2026, 9, 24),
                                  opening_balance_before_today_spend=1000, spent_today=spent,
                                  income_by_date={}, mandatory_by_date={})
    assert plan.daily_target == expected_daily
    assert plan.available_today == available
    assert plan.capital_shortfall == shortfall
    assert plan.buffer_balance == 900
    assert plan.projected_end_balance == round(1000 - spent - 9 * expected_daily, 2)


def test_rounding_never_spends_nonexistent_kopecks():
    plan = calculate_cashflow_plan(today=date(2026, 9, 15), horizon_end=date(2026, 9, 20),
                                  opening_balance_before_today_spend=100, spent_today=0,
                                  income_by_date={}, mandatory_by_date={})
    assert plan.daily_target == 16.66
    assert plan.minimum_projected_balance == 0.04


def test_period_table_includes_overspend(client):
    result = cashflow_period_rows(1, today=date(2026, 9, 15), horizon_end=date(2026, 9, 20),
                                 opening_balance_before_today_spend=1000, daily_target=20, spent_today=900)
    assert result[0]['buffer'] == 0


def test_overspend_does_not_reduce_current_period_buffer(client):
    before = cashflow_period_rows(
        1, today=date(2026, 9, 15), horizon_end=date(2026, 9, 20),
        opening_balance_before_today_spend=1000, daily_target=100, today_target=100,
    )
    after = cashflow_period_rows(
        1, today=date(2026, 9, 15), horizon_end=date(2026, 9, 20),
        opening_balance_before_today_spend=1000, daily_target=20, today_target=100, spent_today=900,
    )
    assert after[0]['buffer'] == before[0]['buffer']


def test_yesterday_overspend_is_not_subtracted_again_after_midnight(client, monkeypatch):
    monkeypatch.setattr(clock, 'today', lambda: date(2026, 9, 17))
    with connect() as con:
        con.execute(
            "UPDATE settings SET cashflow_start_date='2026-09-17',"
            "cashflow_start_capital=39000 WHERE user_id=1"
        )

    bill = client.post('/api/bill-rules', json={
        'title': 'Подписка', 'amount': 1753.55, 'day_of_month': 17,
        'effective_date': '2026-09-17',
    }).json()
    assert client.post(
        f"/api/bills/{bill['id']}/pay", json={'due_date': '2026-09-17'}
    ).status_code == 200
    before = cashflow_snapshot(1)

    for amount in (120, 1445.83, 713.98):
        assert client.post('/api/transactions', json={
            'type': 'expense', 'amount': amount, 'tx_date': '2026-09-17',
        }).status_code == 200

    monkeypatch.setattr(clock, 'today', lambda: date(2026, 9, 18))
    assert client.post('/api/transactions', json={
        'type': 'expense', 'amount': 120, 'tx_date': '2026-09-18',
    }).status_code == 200
    after = cashflow_snapshot(1)

    total_spent = 120 + 1445.83 + 713.98 + 120
    assert after['buffer_balance'] == before['buffer_balance']
    assert after['remaining_period'] == round(before['period_budget'] - total_spent, 2)
    assert after['current_cash'] == round(39000 - 1753.55 - total_spent, 2)
    assert after['remaining_period'] + after['buffer_balance'] == after['available_cash']
    days_left = (
        date.fromisoformat(after['periods'][0]['end'])
        - date.fromisoformat(after['today'])
    ).days + 1
    expected_today = (
        round((after['remaining_period'] + 120) * 100) // days_left
    ) / 100
    assert after['today_target'] == expected_today
    assert after['available_today'] == round(expected_today - 120, 2)
    assert after['today_target'] * days_left <= after['remaining_period'] + 120
    assert after['periods'][0]['daily'] == after['daily_target']
    assert after['periods'][0]['buffer'] == after['buffer_balance']
    assert after['periods'][0]['put_aside'] == after['buffer_balance']


def test_actual_cash_and_unpaid_obligations_are_separate(client):
    bill = client.post('/api/bill-rules', json={
        'title': 'Подписка', 'amount': 100, 'day_of_month': 15,
        'effective_date': '2026-09-01',
    }).json()
    before = cashflow_snapshot(1)
    assert before['current_cash'] == 1000
    assert before['reserved_mandatory'] == 100
    assert before['available_cash'] == 900
    assert before['mandatory_period'] == 100

    assert client.post(f"/api/bills/{bill['id']}/pay", json={'due_date': '2026-09-15'}).status_code == 200
    after = cashflow_snapshot(1)
    assert after['current_cash'] == 900
    assert after['reserved_mandatory'] == 0
    assert after['available_cash'] == 900
    assert after['mandatory_period'] == 0


def test_early_bill_payment_is_not_charged_again_on_due_date(client):
    bill = client.post('/api/bill-rules', json={
        'title': 'Будущий счёт', 'amount': 100, 'day_of_month': 20,
        'effective_date': '2026-09-01',
    }).json()
    payment = client.post(f"/api/bills/{bill['id']}/pay", json={'due_date': '2026-09-20'})
    assert payment.status_code == 200
    assert payment.json()['tx_date'] == '2026-09-15'
    snapshot = cashflow_snapshot(1)
    assert snapshot['current_cash'] == 900
    assert all(row['mandatory'] == 0 for row in snapshot['timeline'] if row['date'] == '2026-09-20')


def test_manual_transaction_can_be_edited_but_not_future_dated(client):
    created = client.post('/api/transactions', json={
        'type': 'expense', 'amount': 50, 'tx_date': '2026-09-15', 'note': 'Кофе',
    })
    assert created.status_code == 200
    tx_id = created.json()['id']
    edited = client.put(f'/api/transactions/{tx_id}', json={
        'type': 'expense', 'amount': 75, 'tx_date': '2026-09-14', 'note': 'Кофе и десерт',
    })
    assert edited.status_code == 200
    assert edited.json()['amount'] == 75
    assert edited.json()['tx_date'] == '2026-09-14'
    assert client.post('/api/transactions', json={
        'type': 'income', 'amount': 100, 'tx_date': '2026-09-16',
    }).status_code == 422


def test_legacy_vacation_reserve_migrates_into_start_capital(client):
    backup = export_user_data(1)
    backup['backup_version'] = 4
    backup['data']['settings'].pop('cashflow_start_capital')
    backup['data']['settings']['initial_reserve'] = 28000
    backup['data']['settings']['initial_vacation_reserve'] = 11000
    restore_user_data(1, backup)
    settings = export_user_data(1)['data']['settings']
    assert settings['cashflow_start_capital'] == 39000
    assert settings['initial_vacation_reserve'] == 0
    assert cashflow_snapshot(1)['current_cash'] == 39000


def test_future_received_amount_override_recalculates_entire_cashflow(client, monkeypatch):
    with connect() as con:
        con.execute(
            "UPDATE income_rules SET amount=39395.22 WHERE user_id=1 AND kind='advance'"
        )

    before = cashflow_snapshot(1)
    planned = next(row for row in before['periods'] if row['start'] == '2026-09-23')
    assert planned['received'] == 39395.22
    assert planned['income_overridden'] is False

    response = client.put(
        '/api/cashflow/income-overrides/2026-09-23', json={'amount': 41000}
    )
    assert response.status_code == 200
    after = cashflow_snapshot(1)
    corrected = next(row for row in after['periods'] if row['start'] == '2026-09-23')
    assert corrected['planned_received'] == 39395.22
    assert corrected['received'] == 41000
    assert corrected['free'] == 41000
    assert corrected['income_overridden'] is True
    # The near-term balance is still the limiting factor for the daily amount,
    # but the corrected receipt must flow through the rest of the forecast.
    assert after['daily_target'] == before['daily_target']
    assert after['projected_end_balance'] == round(
        before['projected_end_balance'] + 41000 - 39395.22, 2
    )

    monkeypatch.setattr(clock, 'today', lambda: date(2026, 9, 23))
    assert cashflow_snapshot(1)['current_cash'] == 42000


def test_payday_override_does_not_rewrite_closed_payment_day(client, monkeypatch):
    with connect() as con:
        con.execute(
            "UPDATE income_rules SET amount=39395.22 WHERE user_id=1 AND kind='advance'"
        )
    monkeypatch.setattr(clock, 'today', lambda: date(2026, 9, 22))

    before = cashflow_snapshot(1)
    closed_before = next(row for row in before['periods'] if row['start'] == '2026-09-22')
    next_before = next(row for row in before['periods'] if row['start'] == '2026-09-23')

    response = client.put(
        '/api/cashflow/income-overrides/2026-09-23', json={'amount': 41000}
    )
    assert response.status_code == 200

    after = cashflow_snapshot(1)
    closed_after = next(row for row in after['periods'] if row['start'] == '2026-09-22')
    next_after = next(row for row in after['periods'] if row['start'] == '2026-09-23')
    assert closed_after['received'] == closed_before['received']
    assert closed_after['daily'] == closed_before['daily']
    assert closed_after['buffer'] == closed_before['buffer']
    assert next_after['received'] == 41000
    assert next_after['received'] != next_before['received']


def test_income_override_can_be_reset_and_current_row_cannot_be_edited(client):
    assert client.put(
        '/api/cashflow/income-overrides/2026-09-23', json={'amount': 41000}
    ).status_code == 200
    assert client.delete('/api/cashflow/income-overrides/2026-09-23').status_code == 200
    restored = next(
        row for row in cashflow_snapshot(1)['periods'] if row['start'] == '2026-09-23'
    )
    assert restored['income_overridden'] is False
    assert restored['received'] == restored['planned_received']
    assert client.put(
        '/api/cashflow/income-overrides/2026-09-15', json={'amount': 41000}
    ).status_code == 422


def test_income_edits_and_deletes_preserve_past(client):
    rule = salary_rule()
    before = cashflow_snapshot(1)['current_cash']
    assert before == 1000
    rule['amount'] = 20000
    assert client.put(f"/api/income-rules/{rule['id']}", json=rule).status_code == 200
    assert cashflow_snapshot(1)['current_cash'] == before
    assert planning.income_map(1, date(2026, 10, 7), date(2026, 10, 7))[date(2026, 10, 7)] == 20000
    assert client.delete(f"/api/income-rules/{rule['id']}").status_code == 200
    assert cashflow_snapshot(1)['current_cash'] == before
    assert rule['id'] not in [r['id'] for r in client.get('/api/bootstrap').json()['income_rules']]


def test_new_rules_do_not_appear_in_past_unless_explicit(client):
    body = {'title': 'Доход', 'amount': 500, 'day_of_month': 10}
    rule = client.post('/api/income-rules', json=body).json()
    assert date(2026, 9, 10) not in planning.income_map(1, date(2026, 9, 10), date(2026, 9, 10))
    assert cashflow_snapshot(1)['current_cash'] == 1000

    body['effective_date'] = '2026-09-01'
    assert client.put(f"/api/income-rules/{rule['id']}", json=body).status_code == 200
    assert planning.income_map(1, date(2026, 9, 10), date(2026, 9, 10))[date(2026, 9, 10)] == 500
    assert cashflow_snapshot(1)['current_cash'] == 1000


def test_backdated_salary_edit_preserves_unrelated_bill_history(client, monkeypatch):
    monkeypatch.setattr(clock, 'today', lambda: date(2026, 9, 10))
    bill = client.post('/api/bill-rules', json={'title': 'Связь', 'amount': 100, 'day_of_month': 12}).json()
    monkeypatch.setattr(clock, 'today', lambda: date(2026, 9, 15))
    body = {**bill, 'amount': 200}
    client.put(f"/api/bill-rules/{bill['id']}", json=body)
    rule = {**salary_rule(), 'amount': 20000, 'effective_date': '2026-09-01'}
    client.put(f"/api/income-rules/{rule['id']}", json=rule)
    assert planning.mandatory_map(1, date(2026, 9, 1), date(2026, 9, 15)) == {date(2026, 9, 12): 100}
    assert planning.mandatory_map(1, date(2026, 10, 1), date(2026, 10, 15)) == {date(2026, 10, 12): 200}


def test_payroll_settings_preserve_paid_months(client):
    with connect() as con:
        con.execute('UPDATE settings SET payroll_enabled=1,salary_gross=100000 WHERE user_id=1')
    before = cashflow_snapshot(1)['current_cash']
    response = client.put('/api/payroll-settings', json={'payroll_enabled': True, 'salary_gross': 200000})
    assert response.status_code == 200
    assert cashflow_snapshot(1)['current_cash'] == before


def test_paid_bill_amount_and_classification_survive_edit_delete(client):
    body = {'title': 'Связь', 'amount': 100, 'day_of_month': 10, 'effective_date': '2026-09-01'}
    bill = client.post('/api/bill-rules', json=body).json()
    payment = client.post(f"/api/bills/{bill['id']}/pay", json={'due_date': '2026-09-10'})
    assert payment.status_code == 200
    before = cashflow_snapshot(1)['current_cash']
    body.update(amount=200, effective_date='2026-09-01')
    assert client.put(f"/api/bill-rules/{bill['id']}", json=body).status_code == 200
    assert cashflow_snapshot(1)['current_cash'] == before
    client.delete(f"/api/bill-rules/{bill['id']}")
    assert cashflow_snapshot(1)['current_cash'] == before
    with connect() as con:
        assert con.execute('SELECT bill_rule_id FROM transactions WHERE id=?', (payment.json()['id'],)).fetchone()[0] == bill['id']
    assert client.post(f"/api/bills/{bill['id']}/pay", json={'due_date': '2026-09-11'}).status_code == 422


def test_paid_bill_actual_amount_can_return_remainder_to_budget_or_piggy(client):
    bill = client.post('/api/bill-rules', json={
        'title': 'Коммуналка', 'amount': 10000, 'day_of_month': 10, 'effective_date': '2026-09-01'
    }).json()
    payment = client.post(f"/api/bills/{bill['id']}/pay", json={'due_date': '2026-09-10'}).json()
    before = cashflow_snapshot(1)['current_cash']

    response = client.put(f"/api/bill-payments/{payment['id']}", json={
        'amount': 8000, 'remainder_destination': 'budget'
    })
    assert response.status_code == 200
    assert response.json()['remainder_amount'] == 0
    assert planning.mandatory_map(1, date(2026, 9, 10), date(2026, 9, 10)) == {date(2026, 9, 10): 8000}
    assert cashflow_snapshot(1)['current_cash'] == before + 2000
    assert piggy_bank_balance(1) == 0

    response = client.put(f"/api/bill-payments/{payment['id']}", json={
        'amount': 8000, 'remainder_destination': 'piggy'
    })
    assert response.status_code == 200
    assert response.json()['remainder_amount'] == 2000
    assert piggy_bank_balance(1) == 2000
    assert cashflow_snapshot(1)['current_cash'] == before

    response = client.put(f"/api/bill-payments/{payment['id']}", json={
        'amount': 7000, 'remainder_destination': 'piggy'
    })
    assert response.status_code == 200
    assert piggy_bank_balance(1) == 3000
    with connect() as con:
        assert con.execute(
            'SELECT COUNT(*) FROM piggy_bank_movements WHERE bill_payment_id=?', (payment['id'],)
        ).fetchone()[0] == 1

    event = client.get('/api/plan').json()[0]
    assert event['amount'] == 7000
    assert event['planned_amount'] == 10000
    assert event['remainder_destination'] == 'piggy'
    assert event['remainder_amount'] == 3000
    transaction = next(row for row in client.get('/api/transactions').json() if row['id'] == payment['id'])
    assert transaction['bill_planned_amount'] == 10000
    assert transaction['remainder_destination'] == 'piggy'
    assert transaction['remainder_amount'] == 3000


def test_bill_remainder_edit_rolls_back_if_it_would_overdraw_piggy(client):
    bill = client.post('/api/bill-rules', json={
        'title': 'Коммуналка', 'amount': 10000, 'day_of_month': 10, 'effective_date': '2026-09-01'
    }).json()
    payment = client.post(f"/api/bills/{bill['id']}/pay", json={'due_date': '2026-09-10'}).json()
    assert client.put(f"/api/bill-payments/{payment['id']}", json={
        'amount': 8000, 'remainder_destination': 'piggy'
    }).status_code == 200
    assert client.post('/api/piggy-bank/withdraw', json={
        'amount': 1500, 'movement_date': '2026-09-15'
    }).status_code == 200

    response = client.put(f"/api/bill-payments/{payment['id']}", json={
        'amount': 9500, 'remainder_destination': 'piggy'
    })
    assert response.status_code == 422
    with connect() as con:
        stored = con.execute('SELECT amount FROM transactions WHERE id=?', (payment['id'],)).fetchone()[0]
        saved = con.execute(
            'SELECT amount FROM piggy_bank_movements WHERE bill_payment_id=?', (payment['id'],)
        ).fetchone()[0]
    assert stored == 8000
    assert saved == 2000


def test_confirmed_period_override_replaces_later_income_in_same_period(client, monkeypatch):
    assert client.post('/api/income-rules', json={
        'title': 'Доход внутри периода', 'amount': 500, 'day_of_month': 25,
        'kind': 'other', 'effective_date': '2026-09-01',
    }).status_code == 200
    assert client.put(
        '/api/cashflow/income-overrides/2026-09-23', json={'amount': 12000}
    ).status_code == 200

    monkeypatch.setattr(clock, 'today', lambda: date(2026, 9, 23))
    snapshot = cashflow_snapshot(1)
    assert snapshot['current_cash'] == 13000
    assert not any(
        '2026-09-23' <= row['date'] <= '2026-10-07' and row['income']
        for row in snapshot['timeline']
    )


def test_bill_remainder_link_survives_backup_restore(client):
    bill = client.post('/api/bill-rules', json={
        'title': 'Коммуналка', 'amount': 10000, 'day_of_month': 10, 'effective_date': '2026-09-01'
    }).json()
    payment = client.post(f"/api/bills/{bill['id']}/pay", json={'due_date': '2026-09-10'}).json()
    client.put(f"/api/bill-payments/{payment['id']}", json={
        'amount': 8000, 'remainder_destination': 'piggy'
    })
    backup = export_user_data(1)
    ensure_user(2)
    restore_user_data(2, backup)

    assert piggy_bank_balance(2) == 2000
    with connect() as con:
        linked = con.execute(
            'SELECT t.amount,p.amount FROM transactions t JOIN piggy_bank_movements p '
            'ON p.bill_payment_id=t.id WHERE t.user_id=2'
        ).fetchone()
    assert tuple(linked) == (8000, 2000)


def test_history_boundaries_do_not_rewrite_previous_payday(client):
    rule = {**salary_rule(), 'day_of_month': 16}
    client.put(f"/api/income-rules/{rule['id']}", json=rule)
    period = planning.user_period(1, date(2026, 9, 15))
    assert period.start == date(2026, 9, 8)
    assert period.end == date(2026, 9, 16)


def test_backup_restores_piggy_to_new_and_existing_users(client):
    assert client.post('/api/piggy-bank/deposit', json={'amount': 300, 'movement_date': '2026-09-12'}).status_code == 200
    backup = export_user_data(1)
    ensure_user(2)
    restore_user_data(2, backup)
    assert piggy_bank_balance(2) == 300
    client.post('/api/piggy-bank/deposit', json={'amount': 100, 'movement_date': '2026-09-13'})
    restore_user_data(1, backup)
    assert piggy_bank_balance(1) == 300


def test_backup_restores_cashflow_income_overrides(client):
    assert client.put(
        '/api/cashflow/income-overrides/2026-09-23', json={'amount': 41000}
    ).status_code == 200
    backup = export_user_data(1)
    assert backup['backup_version'] == 7
    assert backup['data']['cashflow_income_overrides'][0]['amount'] == 41000
    ensure_user(2)
    restore_user_data(2, backup)
    corrected = next(
        row for row in cashflow_snapshot(2)['periods'] if row['start'] == '2026-09-23'
    )
    assert corrected['received'] == 41000
    assert corrected['income_overridden'] is True


def test_backup_remaps_archived_rule_history(client):
    rule = {**salary_rule(), 'amount': 20000}
    client.put(f"/api/income-rules/{rule['id']}", json=rule)
    client.delete(f"/api/income-rules/{rule['id']}")
    backup = export_user_data(1)
    ensure_user(2)
    restore_user_data(2, backup)
    assert cashflow_snapshot(2)['current_cash'] == cashflow_snapshot(1)['current_cash'] == 1000
    # A second export/import must remain valid after ID remapping.
    restore_user_data(2, export_user_data(2))
    assert cashflow_snapshot(2)['current_cash'] == 1000


def test_version_one_backup_without_piggy_replaces_all_data(client):
    backup = export_user_data(1)
    backup['backup_version'] = 1
    del backup['data']['piggy_bank_movements']
    del backup['data']['plan_history']
    client.post('/api/piggy-bank/deposit', json={'amount': 300, 'movement_date': '2026-09-12'})
    restore_user_data(1, backup)
    assert piggy_bank_balance(1) == 0


@pytest.mark.parametrize('field,value', [('salary_day', 0), ('advance_day', 32), ('forecast_months', 0), ('tax_rate', 101), ('cashflow_start_date', 'broken'), ('salary_gross', float('inf'))])
def test_invalid_backup_never_changes_existing_data(client, field, value):
    before = export_user_data(1)['data']
    payload = export_user_data(1)
    payload['data']['settings'][field] = value
    with pytest.raises(ValueError):
        restore_user_data(1, payload)
    assert export_user_data(1)['data'] == before


def test_invalid_backup_references_rejected(client):
    backup = export_user_data(1)
    backup['data']['transactions'] = [{'id': 1, 'type': 'expense', 'amount': 1, 'tx_date': '2026-09-15', 'category_id': 99999}]
    assert client.post('/api/backup/restore', json=backup).status_code == 422
    assert client.get('/api/dashboard').status_code == 200


def test_legacy_dashboard_only_tracks_explicit_daily_budget_piggy_transfers(client):
    client.put('/api/cashflow-settings', json={'cashflow_enabled': False, 'start_date': '2026-09-01', 'start_capital': 1000})
    before = client.get('/api/dashboard').json()['remaining']
    client.post('/api/piggy-bank/deposit', json={'amount': 1000, 'movement_date': '2026-09-15'})
    assert client.get('/api/dashboard').json()['remaining'] == before
    client.post('/api/piggy-bank/withdraw', json={'amount': 400, 'movement_date': '2026-09-15'})
    assert client.get('/api/dashboard').json()['remaining'] == before


@pytest.mark.parametrize('operation', ['expense', 'new_bill', 'edit_bill'])
def test_foreign_categories_rejected(client, operation):
    ensure_user(2)
    with connect() as con:
        category = con.execute('SELECT id FROM categories WHERE user_id=2 LIMIT 1').fetchone()[0]
    body = {'title': 'Тест', 'amount': 1, 'day_of_month': 15, 'category_id': category}
    if operation == 'expense':
        response = client.post('/api/transactions', json={'type': 'expense', 'amount': 1, 'category_id': category})
    elif operation == 'new_bill':
        response = client.post('/api/bill-rules', json=body)
    else:
        bill = client.post('/api/bill-rules', json={**body, 'category_id': None}).json()
        response = client.put(f"/api/bill-rules/{bill['id']}", json=body)
    assert response.status_code == 422


def test_categories_can_be_reordered_and_new_category_is_appended(client):
    before = client.get('/api/bootstrap').json()['categories']
    reversed_ids = [row['id'] for row in reversed(before)]
    response = client.put('/api/categories/order', json={'category_ids': reversed_ids})
    assert response.status_code == 200
    after = client.get('/api/bootstrap').json()['categories']
    assert [row['id'] for row in after] == reversed_ids

    created = client.post('/api/categories', json={'title': 'Частая', 'emoji': '⭐'}).json()
    final = client.get('/api/bootstrap').json()['categories']
    assert final[-1]['id'] == created['id']


def test_category_order_rejects_incomplete_or_duplicate_lists(client):
    ids = [row['id'] for row in client.get('/api/bootstrap').json()['categories']]
    assert client.put('/api/categories/order', json={'category_ids': ids[:-1]}).status_code == 422
    assert client.put('/api/categories/order', json={'category_ids': [ids[0], *ids]}).status_code == 422


def test_backdated_withdrawal_and_removal_preserve_running_balance(client):
    deposit = client.post('/api/piggy-bank/deposit', json={'amount': 300, 'movement_date': '2026-09-12'}).json()['movement']
    assert client.post('/api/piggy-bank/withdraw', json={'amount': 100, 'movement_date': '2026-09-01'}).status_code == 422
    assert client.post('/api/piggy-bank/withdraw', json={'amount': 200, 'movement_date': '2026-09-13'}).status_code == 200
    client.post('/api/piggy-bank/deposit', json={'amount': 1000, 'movement_date': '2026-09-14'})
    assert client.delete(f"/api/piggy-bank/movements/{deposit['id']}").status_code == 422
    assert piggy_bank_balance(1) == 1100


def test_simultaneous_withdrawals_cannot_overdraw(client):
    client.post('/api/piggy-bank/deposit', json={'amount': 100, 'movement_date': '2026-09-12'})
    def withdraw():
        try:
            add_piggy_bank_movement(1, 'withdraw', PiggyBankMovementIn(amount=80, movement_date=date(2026, 9, 15)))
            return 200
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: withdraw(), range(2))) == [200, 422]
    assert piggy_bank_balance(1) == 20


def test_telegram_required_when_dev_mode_not_set(monkeypatch):
    monkeypatch.delenv('DEV_MODE', raising=False)
    monkeypatch.setenv('BOT_TOKEN', 'dummy:test')
    with pytest.raises(HTTPException) as exc:
        asyncio.run(current_user(None))
    assert exc.value.status_code == 401


def test_clock_uses_budget_timezone_at_midnight(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz):
            return datetime(2026, 9, 14, 21, 30, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(clock, 'datetime', FixedDatetime)
    monkeypatch.setenv('BUDGET_TIMEZONE', 'Europe/Moscow')
    assert clock.today() == date(2026, 9, 15)
    monkeypatch.setenv('BUDGET_TIMEZONE', 'Asia/Vladivostok')
    assert clock.today() == date(2026, 9, 15)


def test_existing_database_migration_preserves_data(client):
    with connect() as con:
        con.execute('DROP TABLE plan_history')
        con.execute('ALTER TABLE income_rules DROP COLUMN archived')
        con.execute('ALTER TABLE bill_rules DROP COLUMN archived')
    init_db()
    init_db()
    assert cashflow_snapshot(1)['current_cash'] == 1000
    assert salary_rule()['amount'] == 10000


def test_payment_columns_are_added_before_linked_piggy_index(tmp_path, monkeypatch):
    database = tmp_path / 'legacy-budget.sqlite3'
    monkeypatch.setenv('DATABASE_PATH', str(database))
    with sqlite3.connect(database) as con:
        con.executescript('''
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                tx_date TEXT NOT NULL,
                category_id INTEGER,
                note TEXT NOT NULL DEFAULT '',
                bill_rule_id INTEGER,
                bill_due_date TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE piggy_bank_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                amount REAL NOT NULL,
                movement_date TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        ''')

    init_db()
    init_db()
    with sqlite3.connect(database) as con:
        transaction_columns = {row[1] for row in con.execute('PRAGMA table_info(transactions)')}
        piggy_columns = {row[1] for row in con.execute('PRAGMA table_info(piggy_bank_movements)')}
        indexes = {row[1] for row in con.execute('PRAGMA index_list(piggy_bank_movements)')}
    assert 'bill_planned_amount' in transaction_columns
    assert 'bill_payment_id' in piggy_columns
    assert 'ux_piggy_bill_payment' in indexes


def test_backup_restores_bill_history_and_paid_links(client):
    rule = client.post('/api/bill-rules', json={'title':'Связь','amount':100,'day_of_month':10,'effective_date':'2026-09-01'}).json()
    client.post(f"/api/bills/{rule['id']}/pay", json={'due_date':'2026-09-10'})
    client.delete(f"/api/bill-rules/{rule['id']}")
    ensure_user(2)
    restore_user_data(2, export_user_data(1))
    assert cashflow_snapshot(2)['current_cash'] == cashflow_snapshot(1)['current_cash'] == 900
    events = planning.bill_events(2, date(2026, 9, 1), date(2026, 9, 15))
    assert len(events) == 1
    assert events[0]['amount'] == 100
    assert events[0]['paid'] is True
