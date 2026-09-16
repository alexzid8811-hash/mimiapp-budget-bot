import asyncio
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
        con.execute("UPDATE settings SET cashflow_enabled=1,cashflow_start_date='2026-09-01',initial_reserve=1000,forecast_months=1 WHERE user_id=1")
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


@pytest.mark.parametrize('spent,expected_daily,shortfall,buffer', [(900, 11.11, 0, 100), (1100, 0, 100, 0)])
def test_overspend_uses_actual_cash(spent, expected_daily, shortfall, buffer):
    plan = calculate_cashflow_plan(today=date(2026, 9, 15), horizon_end=date(2026, 9, 24),
                                  opening_balance_before_today_spend=1000, spent_today=spent,
                                  income_by_date={}, mandatory_by_date={})
    assert plan.daily_target == expected_daily
    assert plan.available_today == 0
    assert plan.capital_shortfall == shortfall
    assert plan.buffer_balance == buffer
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
    assert cashflow_snapshot(1)['current_cash'] == 1000
    body['effective_date'] = '2026-09-01'
    assert client.put(f"/api/income-rules/{rule['id']}", json=body).status_code == 200
    assert cashflow_snapshot(1)['current_cash'] == 11500


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


def test_history_boundaries_do_not_rewrite_previous_payday(client):
    rule = {**salary_rule(), 'day_of_month': 16}
    client.put(f"/api/income-rules/{rule['id']}", json=rule)
    period = planning.user_period(1, date(2026, 9, 15))
    assert period.start == date(2026, 9, 7)
    assert period.end == date(2026, 9, 15)


def test_backup_restores_piggy_to_new_and_existing_users(client):
    assert client.post('/api/piggy-bank/deposit', json={'amount': 300, 'movement_date': '2026-09-12'}).status_code == 200
    backup = export_user_data(1)
    ensure_user(2)
    restore_user_data(2, backup)
    assert piggy_bank_balance(2) == 300
    client.post('/api/piggy-bank/deposit', json={'amount': 100, 'movement_date': '2026-09-13'})
    restore_user_data(1, backup)
    assert piggy_bank_balance(1) == 300


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


def test_legacy_dashboard_tracks_piggy(client):
    client.put('/api/cashflow-settings', json={'cashflow_enabled': False, 'start_date': '2026-09-01', 'start_capital': 1000})
    before = client.get('/api/dashboard').json()['remaining']
    client.post('/api/piggy-bank/deposit', json={'amount': 1000, 'movement_date': '2026-09-15'})
    assert client.get('/api/dashboard').json()['remaining'] == before - 1000
    client.post('/api/piggy-bank/withdraw', json={'amount': 400, 'movement_date': '2026-09-15'})
    assert client.get('/api/dashboard').json()['remaining'] == before - 600


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


def test_backup_restores_bill_history_and_paid_links(client):
    rule = client.post('/api/bill-rules', json={'title':'Связь','amount':100,'day_of_month':10,'effective_date':'2026-09-01'}).json()
    client.post(f"/api/bills/{rule['id']}/pay", json={'due_date':'2026-09-10'})
    client.delete(f"/api/bill-rules/{rule['id']}")
    ensure_user(2)
    restore_user_data(2, export_user_data(1))
    assert cashflow_snapshot(2)['current_cash'] == cashflow_snapshot(1)['current_cash'] == 10900
    events = planning.bill_events(2, date(2026, 9, 1), date(2026, 9, 15))
    assert len(events) == 1
    assert events[0]['amount'] == 100
    assert events[0]['paid'] is True
