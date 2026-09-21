"""Validate the entire backup before deleting any current user data."""
import json
from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from . import clock
from .validation import APIModel
from .savings import validate_piggy_history


class Payroll(APIModel):
    payroll_enabled: bool = False
    salary_gross: float = Field(default=0, ge=0)
    bonus_gross: float = Field(default=0, ge=0)
    tax_rate: float = Field(default=13, ge=0, le=100)
    salary_day: int = Field(default=7, ge=1, le=31)
    advance_day: int = Field(default=22, ge=1, le=31)


class Settings(Payroll):
    currency: str = Field(default='RUB', pattern=r'^[A-Z]{3}$')
    initial_reserve: float = Field(default=0, ge=0)
    initial_vacation_reserve: float = Field(default=0, ge=0)
    forecast_months: int = Field(default=4, ge=1, le=12)
    cashflow_enabled: bool = False
    cashflow_start_date: date | None = None
    cashflow_start_capital: float | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def valid_start(self):
        if self.cashflow_start_date and self.cashflow_start_date > clock.today():
            raise ValueError('Дата старта в будущем')
        return self


class Record(APIModel):
    id: int = Field(gt=0)
    created_at: datetime | None = None


class Category(Record):
    title: str = Field(min_length=1, max_length=40)
    emoji: str = Field(default='💳', min_length=1, max_length=8)
    sort_order: int = Field(default=0, ge=0)


class Income(Record):
    title: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0)
    day_of_month: int = Field(ge=1, le=31)
    kind: Literal['salary', 'advance', 'other'] = 'other'
    is_payday: bool = False
    active: bool = True
    archived: bool = False


class Bill(Record):
    title: str = Field(min_length=1, max_length=80)
    amount: float = Field(ge=0)
    day_of_month: int = Field(ge=1, le=31)
    category_id: int | None = Field(default=None, gt=0)
    active: bool = True
    archived: bool = False


class Transaction(Record):
    type: Literal['expense', 'income']
    amount: float = Field(ge=0)
    tx_date: date
    category_id: int | None = Field(default=None, gt=0)
    note: str = Field(default='', max_length=200)
    bill_rule_id: int | None = Field(default=None, gt=0)
    bill_due_date: date | None = None
    bill_planned_amount: float | None = Field(default=None, ge=0)
    income_destination: Literal['daily', 'buffer', 'piggy'] = 'daily'

    @model_validator(mode='after')
    def bill_fields(self):
        if self.bill_rule_id is not None and (self.type != 'expense' or self.bill_due_date is None):
            raise ValueError('Неверная связь обязательного платежа')
        if self.tx_date > clock.today():
            raise ValueError('Дата операции в будущем')
        return self


class Vacation(Record):
    start_date: date
    end_date: date
    amount: float = Field(ge=0)
    payment_date: date
    note: str = Field(default='', max_length=120)

    @model_validator(mode='after')
    def valid_range(self):
        if self.end_date < self.start_date:
            raise ValueError('Неверные даты отпуска')
        return self


class Reserve(Record):
    period_start: date
    amount: float
    reason: str = ''
    source: Literal['auto', 'manual'] = 'auto'


class Piggy(Record):
    direction: Literal['deposit', 'withdraw']
    amount: float = Field(gt=0)
    movement_date: date
    note: str = Field(default='', max_length=160)
    source: Literal['external', 'daily_budget'] = 'external'
    bill_payment_id: int | None = Field(default=None, gt=0)
    income_transaction_id: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def not_future(self):
        if self.movement_date > clock.today():
            raise ValueError('Дата операции копилки в будущем')
        return self


class CashflowIncomeOverride(Record):
    period_start: date
    amount: float = Field(ge=0)
    updated_at: datetime | None = None


class Conditions(APIModel):
    settings: Payroll
    income_rules: list[Income]
    bill_rules: list[Bill]


class History(APIModel):
    id: int = Field(gt=0)
    effective_date: date
    snapshot: str | dict


MODELS = {'categories': Category, 'income_rules': Income, 'bill_rules': Bill,
          'transactions': Transaction, 'vacations': Vacation, 'reserve_movements': Reserve,
          'piggy_bank_movements': Piggy, 'cashflow_income_overrides': CashflowIncomeOverride,
          'plan_history': History}


def validate_backup(payload):
    if not isinstance(payload, dict) or type(payload.get('backup_version')) is not int or payload['backup_version'] not in (1, 2, 3, 4, 5, 6, 7):
        raise ValueError('Неподдерживаемая версия резервной копии')
    if payload.get('app') != 'mimiapp-budget-bot':
        raise ValueError('Этот файл создан другим приложением')
    source = payload.get('data')
    if not isinstance(source, dict) or not isinstance(source.get('settings'), dict):
        raise ValueError('В резервной копии нет настроек')
    data = {'settings': Settings.model_validate(source['settings']).model_dump(mode='json')}
    for table, model in MODELS.items():
        optional_legacy_tables = {'cashflow_income_overrides'}
        if payload['backup_version'] == 1:
            optional_legacy_tables.update({'piggy_bank_movements', 'plan_history'})
        records = source.get(table, [] if table in optional_legacy_tables else None)
        if not isinstance(records, list):
            raise ValueError(f'Повреждён раздел {table}')
        data[table] = [model.model_validate(r).model_dump(mode='json') for r in records]
        ids = [r['id'] for r in data[table]]
        if len(set(ids)) != len(ids):
            raise ValueError(f'Повторяющиеся идентификаторы в {table}')
        data[table].sort(key=lambda row: row['id'])
    category_ids = {r['id'] for r in data['categories']}
    bill_ids = {r['id'] for r in data['bill_rules']}
    income_ids = {r['id'] for r in data['income_rules']}

    def category_ref(row):
        if row.get('category_id') is not None and row['category_id'] not in category_ids:
            raise ValueError('Не найдена категория из резервной копии')

    for row in data['bill_rules'] + data['transactions']:
        category_ref(row)
    for row in data['transactions']:
        if row['bill_rule_id'] is not None and row['bill_rule_id'] not in bill_ids:
            raise ValueError('Не найдено правило обязательного платежа')
    transaction_by_id = {row['id']: row for row in data['transactions']}
    linked_payments = set()
    for row in data['piggy_bank_movements']:
        payment_id = row.get('bill_payment_id')
        if payment_id is None:
            continue
        payment = transaction_by_id.get(payment_id)
        if payment is None or payment['bill_rule_id'] is None or payment['type'] != 'expense':
            raise ValueError('Не найден оплаченный обязательный платёж для копилки')
        if payment_id in linked_payments:
            raise ValueError('Повторяющаяся связь остатка платежа с копилкой')
        linked_payments.add(payment_id)
    dates = set()
    for row in data['plan_history']:
        effective = row['effective_date']
        if effective in dates or effective > clock.today().isoformat():
            raise ValueError('Неверная дата истории условий')
        dates.add(effective)
        raw = json.loads(row['snapshot']) if isinstance(row['snapshot'], str) else row['snapshot']
        conditions = Conditions.model_validate(raw).model_dump(mode='json')
        for table, known in [('income_rules', income_ids), ('bill_rules', bill_ids)]:
            ids = [r['id'] for r in conditions[table]]
            if len(set(ids)) != len(ids) or not set(ids) <= known:
                raise ValueError('Неверные ссылки в истории условий')
        for bill in conditions['bill_rules']:
            category_ref(bill)
        row['snapshot'] = conditions
    if dates and min(dates) != '0001-01-01':
        raise ValueError('Нет начального состояния истории условий')
    validate_piggy_history(data['piggy_bank_movements'])
    return data
