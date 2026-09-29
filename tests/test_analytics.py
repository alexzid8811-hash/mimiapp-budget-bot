from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.analytics import summary
from app.cashflow_app import app
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
        cats = {r['title']: r['id'] for r in con.execute("SELECT id,title FROM categories WHERE user_id=1")}
        bill = con.execute(
            "INSERT INTO bill_rules(user_id,title,amount,day_of_month) VALUES(1,'Аренда',3000,10)"
        ).lastrowid
        con.executemany(
            "INSERT INTO transactions(user_id,type,amount,tx_date,category_id,note) VALUES(1,?,?,?,?,?)",
            [
                ('expense', 400, '2026-09-02', cats['Продукты'], 'магазин'),
                ('expense', 250.5, '2026-09-02', cats['Продукты'], ''),
                ('expense', 900, '2026-09-12', cats['Кафе'], 'ужин'),
                ('expense', 100, '2026-09-13', None, ''),
                ('expense', 500, '2026-08-20', cats['Продукты'], 'август'),
                ('income', 5000, '2026-09-03', None, 'подарок'),
            ],
        )
        con.execute(
            "INSERT INTO transactions(user_id,type,amount,tx_date,bill_rule_id,bill_due_date,bill_planned_amount,note)"
            " VALUES(1,'expense',3000,'2026-09-10',?,'2026-09-10',3000,'Аренда')", (bill,)
        )
        con.executemany(
            "INSERT INTO piggy_bank_movements(user_id,direction,amount,movement_date) VALUES(1,?,?,?)",
            [('deposit', 700, '2026-09-05'), ('withdraw', 200, '2026-09-06'), ('deposit', 50, '2026-10-01')],
        )
    with TestClient(app) as value:
        yield value


def test_month_summary_splits_spending_by_destination_and_category(client):
    data = client.get('/api/analytics', params={'mode': 'month', 'anchor': '2026-09-10'}).json()
    assert data['label'] == 'Сентябрь 2026'
    assert (data['start'], data['end']) == ('2026-09-01', '2026-09-30')
    assert data['totals'] == {'daily': 1650.5, 'bills': 3000.0, 'piggy': 700.0, 'total': 5350.5}
    # September is in progress (today is the 15th): August is compared up to the 15th.
    assert data['previous_partial'] is True
    assert data['previous_totals']['daily'] == 0
    assert data['next_anchor'] is None

    cats = data['categories']
    assert [c['title'] for c in cats] == ['Кафе', 'Продукты', 'Без категории']
    food = cats[1]
    assert food['amount'] == 650.5 and food['previous'] == 0 and food['count'] == 2
    assert [op['amount'] for op in food['top']] == [400, 250.5]
    assert round(sum(c['share'] for c in cats), 3) == 1

    assert len(data['days']) == 30
    assert data['days'][1] == {'date': '2026-09-02', 'amount': 650.5}
    assert [m['month'] for m in data['months']] == ['2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09']
    assert data['months'][-1] == {'month': '2026-09', 'label': 'Сентябрь', 'daily': 1650.5, 'bills': 3000.0, 'piggy': 700.0}
    assert data['bills'] == [{'title': 'Аренда', 'paid_amount': 3000.0, 'due_amount': 0.0, 'count': 1, 'paid_count': 1, 'next_due': None, 'next_amount': 0.0}]


def test_previous_month_can_step_forward_and_future_anchor_is_clamped(client):
    data = client.get('/api/analytics', params={'mode': 'month', 'anchor': '2026-08-01'}).json()
    assert data['totals']['daily'] == 500
    assert data['previous_partial'] is False
    assert data['next_anchor'] == '2026-09-01'
    future = client.get('/api/analytics', params={'mode': 'month', 'anchor': '2027-01-01'}).json()
    assert future['label'] == 'Сентябрь 2026'


def test_year_summary_has_twelve_months_and_no_days(client):
    data = summary(1, 'year', date(2026, 9, 15), date(2026, 9, 15))
    assert data['label'] == '2026 год'
    assert data['days'] == []
    assert len(data['months']) == 12
    assert data['totals']['piggy'] == 750
    # Unpaid months before the current card period are history, not plans.
    rent = data['bills'][0]
    assert (rent['count'], rent['paid_count'], rent['paid_amount'], rent['due_amount']) == (4, 1, 3000, 9000)
    # The row shows three payments left, not one 9 000 payment on 10 October.
    assert (rent['next_due'], rent['next_amount']) == ('2026-10-10', 3000)


def test_card_period_runs_from_payday_to_payday(client):
    data = client.get('/api/analytics', params={'mode': 'period'}).json()
    assert data['start'] <= '2026-09-15' <= data['end']
    assert data['days'][0]['date'] == data['start']
    assert data['label'].endswith('сентября')


def test_unknown_mode_is_rejected(client):
    assert client.get('/api/analytics', params={'mode': 'week'}).status_code == 422


def test_in_progress_month_is_compared_with_the_same_days(client):
    with connect() as con:
        con.execute("INSERT INTO transactions(user_id,type,amount,tx_date,note) VALUES(1,'expense',80,'2026-08-14','')")
    data = client.get('/api/analytics', params={'mode': 'month'}).json()
    # 14 August counts, 20 August (after "today" minus a month) does not.
    assert data['previous_totals']['daily'] == 80
