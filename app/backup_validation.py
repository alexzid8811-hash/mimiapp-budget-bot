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
    morning_report_time: str = Field(default='09:00', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    budget_mode: Literal['payroll', 'irregular'] = 'payroll'
    irregular_reserve_percent: float = Field(default=10, ge=0, le=100)
    irregular_reserve_target: float | None = Field(default=None, ge=0)
    irregular_stretch_days: int = Field(default=14, ge=3, le=60)
    irregular_bills_lookahead_days: int = Field(default=30, ge=0, le=90)
    irregular_bills_scope: Literal['month', 'days'] = 'month'
    irregular_start_date: date | None = None
    irregular_start_total: float = Field(default=0, ge=0)
    irregular_start_reserve: float = Field(default=0, ge=0)
    # Cards and accounts (v11).
    accounts_enabled: bool = False
    main_account_id: int | None = Field(default=None, gt=0)
    accounts_disabled_at: str | None = Field(default=None, max_length=40)
    # «Минимум буфера».
    buffer_floor: float = Field(default=0, ge=0)

    @model_validator(mode='after')
    def valid_start(self):
        if self.cashflow_start_date and self.cashflow_start_date > clock.today():
            raise ValueError('Дата старта в будущем')
        if self.irregular_start_date and self.irregular_start_date > clock.today():
            raise ValueError('Дата старта режима подработок в будущем')
        if self.irregular_start_reserve > self.irregular_start_total:
            raise ValueError('Резерв на старте больше всех денег')
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
    # A planned credit card repayment (v11): one-off, repays this card.
    once_date: date | None = None
    credit_account_id: int | None = Field(default=None, gt=0)
    # A yearly payment: due on day_of_month of this month only.
    month_of_year: int | None = Field(default=None, ge=1, le=12)


class Transaction(Record):
    type: Literal['expense', 'income']
    amount: float = Field(ge=0)
    tx_date: date
    category_id: int | None = Field(default=None, gt=0)
    note: str = Field(default='', max_length=200)
    bill_rule_id: int | None = Field(default=None, gt=0)
    bill_due_date: date | None = None
    bill_planned_amount: float | None = Field(default=None, ge=0)
    income_destination: Literal['daily', 'buffer', 'piggy', 'split', 'reserve'] = 'daily'
    reserve_percent: float | None = Field(default=None, ge=0, le=100)
    income_source: str = Field(default='', max_length=80)
    account_id: int | None = Field(default=None, gt=0)
    funding_envelope: Literal['spending', 'piggy', 'reserve', 'none'] | None = None

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
    purpose: Literal['transfer', 'today', 'cover_overspend', 'from_buffer', 'cap_excess'] | None = None
    bill_payment_id: int | None = Field(default=None, gt=0)
    income_transaction_id: int | None = Field(default=None, gt=0)
    expense_transaction_id: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def not_future(self):
        if self.movement_date > clock.today():
            raise ValueError('Дата операции копилки в будущем')
        return self


class EmergencyReserve(Record):
    direction: Literal['deposit', 'withdraw']
    amount: float = Field(gt=0)
    movement_date: date
    reason: str = Field(default='', max_length=160)
    kind: Literal['deposit', 'from_free', 'pay_expense', 'to_free', 'cover_overspend']
    expense_transaction_id: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def consistent(self):
        if self.movement_date > clock.today():
            raise ValueError('Дата операции резерва в будущем')
        deposit = self.kind in ('deposit', 'from_free')
        if deposit != (self.direction == 'deposit'):
            raise ValueError('Неверное направление операции резерва')
        # A credit card repaid from the reserve is a pay_expense without an
        # expense of its own (checked against the repayment below).
        if self.expense_transaction_id is not None and self.kind != 'pay_expense':
            raise ValueError('Неверная связь операции резерва с расходом')
        return self


class CardAllocation(Record):
    period_start: date
    funded_on: date
    amount: float
    cap_kept: float = Field(default=0, ge=0)


class DailyCap(Record):
    effective_from: date
    amount: float | None = Field(default=None, gt=0)
    topup: float = Field(default=0, ge=0)
    excess_to: Literal['buffer', 'piggy'] = 'buffer'
    moved: float = Field(default=0, ge=0)


class CashflowIncomeOverride(Record):
    period_start: date
    amount: float = Field(ge=0)
    confirmed: bool = False
    updated_at: datetime | None = None


class PayrollChange(Record):
    effective_month: date
    salary_gross: float = Field(ge=0)
    bonus_gross: float = Field(ge=0)

    @model_validator(mode='after')
    def first_of_month(self):
        if self.effective_month.day != 1:
            raise ValueError('Изменение зарплаты начинается с первого дня месяца')
        return self


class Account(Record):
    title: str = Field(min_length=1, max_length=40)
    emoji: str = Field(default='💳', min_length=1, max_length=8)
    kind: Literal['debit', 'savings', 'credit', 'cash']
    opening_balance: float = 0
    opening_date: date
    credit_limit: float | None = Field(default=None, ge=0)
    payment_day: int | None = Field(default=None, ge=1, le=31)
    sort_order: int = Field(default=0, ge=0)
    archived: bool = False


class EnvelopeAccount(APIModel):
    envelope: Literal['spending', 'buffer', 'piggy', 'reserve']
    account_id: int = Field(gt=0)
    assigned_on: date
    assigned_at: str | None = Field(default=None, max_length=40)


class AccountTransfer(Record):
    from_account_id: int = Field(gt=0)
    to_account_id: int | None = Field(default=None, gt=0)
    amount: float = Field(gt=0)
    date: date
    note: str = Field(default='', max_length=160)
    kind: Literal['manual', 'hint', 'linked', 'reassign', 'credit_payment'] = 'manual'
    funding_envelope: Literal['spending', 'buffer', 'piggy', 'reserve', 'none'] | None = None
    interest_amount: float = Field(default=0, ge=0)
    piggy_movement_id: int | None = Field(default=None, gt=0)
    reserve_movement_id: int | None = Field(default=None, gt=0)
    transaction_id: int | None = Field(default=None, gt=0)
    interest_transaction_id: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def consistent(self):
        if self.interest_amount > self.amount:
            raise ValueError('Проценты больше погашения')
        return self


class AccountAdjustment(Record):
    account_id: int = Field(gt=0)
    amount: float
    date: date
    reason: Literal['correction', 'interest', 'cashback'] = 'correction'
    note: str = Field(default='', max_length=160)
    transaction_id: int | None = Field(default=None, gt=0)


class AccountIncome(Record):
    account_id: int = Field(gt=0)
    amount: float = Field(gt=0)
    date: date
    source: Literal['payroll', 'piggy_external', 'reserve_external']
    payroll_period_start: date | None = None
    piggy_movement_id: int | None = Field(default=None, gt=0)
    reserve_movement_id: int | None = Field(default=None, gt=0)
    note: str = Field(default='', max_length=160)


ACCOUNT_TABLES = {'accounts': Account, 'account_transfers': AccountTransfer,
                  'account_adjustments': AccountAdjustment, 'account_incomes': AccountIncome}


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
          'plan_history': History, 'card_allocations': CardAllocation,
          'payroll_changes': PayrollChange, 'emergency_reserve_movements': EmergencyReserve,
          'daily_caps': DailyCap,
          **ACCOUNT_TABLES}


def validate_backup(payload):
    if not isinstance(payload, dict) or type(payload.get('backup_version')) is not int or payload['backup_version'] not in range(1, 13):
        raise ValueError('Неподдерживаемая версия резервной копии')
    if payload.get('app') != 'mimiapp-budget-bot':
        raise ValueError('Этот файл создан другим приложением')
    source = payload.get('data')
    if not isinstance(source, dict) or not isinstance(source.get('settings'), dict):
        raise ValueError('В резервной копии нет настроек')
    data = {'settings': Settings.model_validate(source['settings']).model_dump(mode='json')}
    if payload['backup_version'] < 11:
        # Copies made before cards and accounts restore with the feature off.
        data['settings'].update(accounts_enabled=False, main_account_id=None, accounts_disabled_at=None)
    for table, model in MODELS.items():
        optional_legacy_tables = {'cashflow_income_overrides', 'card_allocations', 'payroll_changes'}
        if payload['backup_version'] < 12:
            optional_legacy_tables.add('daily_caps')
        if payload['backup_version'] < 11:
            optional_legacy_tables.update(ACCOUNT_TABLES)
        if payload['backup_version'] < 10:
            optional_legacy_tables.add('emergency_reserve_movements')
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
    linked_expenses = set()
    for row in data['emergency_reserve_movements']:
        expense_id = row.get('expense_transaction_id')
        if expense_id is None:
            continue
        expense = transaction_by_id.get(expense_id)
        if expense is None or expense['type'] != 'expense' or expense['bill_rule_id'] is not None:
            raise ValueError('Не найден расход, оплаченный из резерва')
        if expense_id in linked_expenses:
            raise ValueError('Повторяющаяся связь расхода с резервом')
        linked_expenses.add(expense_id)
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
    caps = [r['effective_from'] for r in data['daily_caps']]
    if len(set(caps)) != len(caps) or any(day > clock.today().isoformat() for day in caps):
        raise ValueError('Неверная дата максимума в день')
    months = [r['effective_month'] for r in data['payroll_changes']]
    if len(set(months)) != len(months):
        raise ValueError('Повторяющийся месяц изменения зарплаты')
    validate_piggy_history(data['piggy_bank_movements'])
    _validate_accounts(payload['backup_version'], source, data)
    return data


def _validate_accounts(version: int, source: dict, data: dict) -> None:
    """References of the cards and accounts tables (v11)."""
    raw = source.get('envelope_accounts', []) if version >= 11 else []
    if not isinstance(raw, list):
        raise ValueError('Повреждён раздел envelope_accounts')
    data['envelope_accounts'] = [EnvelopeAccount.model_validate(r).model_dump(mode='json') for r in raw]
    accounts = {r['id']: r for r in data['accounts']}
    credit = {i for i, r in accounts.items() if r['kind'] == 'credit'}
    transactions = {r['id']: r for r in data['transactions']}
    piggy = {r['id'] for r in data['piggy_bank_movements']}
    reserve = {r['id']: r for r in data['emergency_reserve_movements']}

    def known(value, ids, what):
        if value is not None and value not in ids:
            raise ValueError(f'Не найден {what} из резервной копии')

    main = data['settings'].get('main_account_id')
    known(main, accounts, 'основной счёт')
    if main in credit:
        raise ValueError('Кредитка не может быть основным счётом')
    if data['settings'].get('accounts_enabled') and main is None:
        raise ValueError('Учёт по картам включён без основного счёта')
    for row in data['transactions']:
        known(row.get('account_id'), accounts, 'счёт операции')
    for row in data['bill_rules']:
        known(row.get('credit_account_id'), credit, 'кредитка платежа')
    for row in data['piggy_bank_movements']:
        expense = row.get('expense_transaction_id')
        if expense is not None and (transactions.get(expense) or {}).get('type') != 'expense':
            raise ValueError('Не найдена трата, оплаченная из копилки')
    envelopes = [r['envelope'] for r in data['envelope_accounts']]
    if len(set(envelopes)) != len(envelopes):
        raise ValueError('Конверт лежит на двух счетах')
    for row in data['envelope_accounts']:
        known(row['account_id'], accounts, 'счёт конверта')
        if row['account_id'] in credit:
            raise ValueError('Конверт не может лежать на кредитке')
    repaid_from_reserve = set()
    for row in data['account_transfers']:
        known(row['from_account_id'], accounts, 'счёт перевода')
        known(row.get('to_account_id'), accounts, 'счёт перевода')
        known(row.get('piggy_movement_id'), piggy, 'операция копилки')
        known(row.get('reserve_movement_id'), reserve, 'операция резерва')
        known(row.get('transaction_id'), transactions, 'платёж погашения')
        known(row.get('interest_transaction_id'), transactions, 'расход «Проценты банка»')
        if row['kind'] == 'credit_payment' and row.get('reserve_movement_id'):
            repaid_from_reserve.add(row['reserve_movement_id'])
    for row in data['account_adjustments']:
        known(row['account_id'], accounts, 'счёт исправления')
        known(row.get('transaction_id'), transactions, 'расход «Проценты банка»')
    for row in data['account_incomes']:
        known(row['account_id'], accounts, 'счёт прихода')
        known(row.get('piggy_movement_id'), piggy, 'операция копилки')
        known(row.get('reserve_movement_id'), reserve, 'операция резерва')
    for movement_id, row in reserve.items():
        if row['kind'] == 'pay_expense' and row.get('expense_transaction_id') is None \
                and movement_id not in repaid_from_reserve:
            raise ValueError('Неверная связь операции резерва с расходом')
