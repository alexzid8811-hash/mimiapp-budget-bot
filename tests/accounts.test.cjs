const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  const classes = new Set();
  return {
    value: '', checked: false, disabled: false, textContent: '', innerHTML: '', style: {}, listeners: {}, dataset: {},
    setAttribute() {}, removeAttribute() {}, replaceChildren() {}, appendChild() {}, reset() {},
    classList: {
      add: (...names) => names.forEach(n => classes.add(n)),
      remove: (...names) => names.forEach(n => classes.delete(n)),
      toggle: (name, force) => {
        const on = force === undefined ? !classes.has(name) : Boolean(force);
        if (on) classes.add(name); else classes.delete(name);
        return on;
      },
      contains: name => classes.has(name),
    },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    showModal() { this.open = true; }, close() { this.open = false; },
  };
}

const ACCOUNTS = {
  enabled: true, today: '2026-10-15', mode: 'payroll', main_account_id: 1, budget_configured: true,
  total: 155000, credit_debt: 0, operations_without_account: 0,
  envelopes: [
    {key: 'spending', label: 'на траты', account_id: 2, amount: 25000},
    {key: 'buffer', label: 'буфер', account_id: 1, amount: 30000},
    {key: 'piggy', label: 'копилка', account_id: 2, amount: 80000},
  ],
  accounts: [
    {id: 1, title: 'Карта 1', emoji: '💳', kind: 'debit', balance: 55000, debt: null, available: null, is_main: true, archived: false, has_operations: true, opening_date: '2026-10-01', envelopes: ['buffer']},
    {id: 2, title: 'Карта 2', emoji: '🏦', kind: 'savings', balance: 100000, debt: null, available: null, is_main: false, archived: false, has_operations: false, opening_date: '2026-10-10', envelopes: ['spending', 'piggy']},
    {id: 3, title: 'Кредитка', emoji: '💳', kind: 'credit', balance: 0, debt: 0, available: 50000, credit_limit: 50000, is_main: false, archived: false, has_operations: false, opening_date: '2026-10-01', envelopes: []},
    {id: 4, title: 'Старая', emoji: '🗂', kind: 'debit', balance: 0, debt: null, available: null, is_main: false, archived: true, has_operations: true, opening_date: '2026-01-01', envelopes: []},
    {id: 5, title: 'Пустая', emoji: '🗂', kind: 'debit', balance: 120, debt: null, available: null, is_main: false, archived: true, has_operations: false, opening_date: '2026-01-01', envelopes: []},
  ],
};

function setup(accounts = ACCOUNTS) {
  const elements = new Map(), requests = [];
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const responses = {
    '/api/bootstrap': { user: {}, budget_timezone: 'Europe/Moscow', settings: {currency:'RUB'}, categories: [{id: 7, title: 'Еда', emoji: '🍔'}], income_rules: [], bill_rules: [] },
    '/api/payroll-settings': {settings: {}}, '/api/vacations': [], '/api/transactions': [], '/api/plan': [],
    '/api/dashboard': {daily_available: 999, period: {start:'2026-10-07',end:'2026-10-21',days_left:7}, reserve: {}, forecast: []},
    '/api/cashflow-settings': {cashflow_enabled:1,start_date:'2026-09-01'},
    '/api/cashflow': {enabled:true,period_budget:100,available_today:5.25,remaining_period:50,buffer_balance:40,current_cash:45.25,periods:[],horizon_end:'2026-11-15',overspend:0,today_target:5.25,period:{start:'2026-10-07',end:'2026-10-21',payday:'2026-10-21',payday_kind:'Выплата'}},
    '/api/piggy-bank': {balance:0,movements:[]},
    '/api/accounts': JSON.parse(JSON.stringify(accounts)),
  };
  const sandbox = {
    console, URLSearchParams, Intl, Date, Number, JSON, Map, Set,
    setTimeout: () => 0, setInterval: () => 0, clearInterval() {},
    confirm: () => true, alert: () => {},
    window: {location:{hash:'',search:''},addEventListener(){},scrollBy(){},innerHeight:800},
    document: {addEventListener(){}, body:{classList:{toggle(){}}}, createElement:()=>({}),getElementById:get, querySelectorAll:()=>[],querySelector:()=>null},
    fetch: async (url, options) => {
      requests.push({url,body:options?.body,method:options?.method});
      const key = url.split('?')[0];
      return {ok:true,status:200,json:async()=>responses[key] ?? {}};
    },
  };
  vm.createContext(sandbox);
  for (const name of ['budget-date.js','money-input.js','app.js','cashflow-ui.js','buffer-ui.js','design-ui.js','irregular-ui.js','accounts-ui.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname,'../app/static',name),'utf8'),sandbox);
  }
  return {sandbox, get, requests, responses};
}

const body = (requests, url) => JSON.parse(requests.filter(r => r.url === url && r.body).at(-1).body);

test('home shows «Всего», the accounts in order and credit cards apart', async () => {
  const {get} = setup();
  await get('refreshBtn').listeners.click();
  assert.equal(get('homeAccounts').classList.contains('hidden'), false);
  assert.match(get('accTotal').textContent, /155.000,00.₽/);
  const list = get('accList').innerHTML;
  assert.ok(list.indexOf('Карта 1') < list.indexOf('Карта 2'));
  assert.match(list, /Карта 1 <i class="acc-main"[^>]*>⭐/);
  assert.doesNotMatch(list, /Кредитка|Старая/);
  assert.match(get('accCredit').innerHTML, /Кредитка[\s\S]*долг 0,00.₽ · доступно 50.000,00.₽/);
});

test('switched off: no block on the home screen and no account in the expense', async () => {
  const {get, requests} = setup({...ACCOUNTS, enabled: false});
  await get('refreshBtn').listeners.click();
  assert.equal(get('homeAccounts').classList.contains('hidden'), true);
  assert.equal(get('accountsSettingsBody').classList.contains('hidden'), true);
  get('quickExpenseBtn').listeners.click();
  assert.equal(get('expenseAccountField').classList.contains('hidden'), true);
  get('expenseAmount').value = '100';
  get('expenseCategory').value = '7';
  await get('expenseForm').listeners.submit({preventDefault(){}, target: get('expenseForm')});
  assert.equal('account_id' in body(requests, '/api/transactions'), false);
});

test('settings list: drag handles for active accounts, a collapsed archive below', async () => {
  const {get} = setup();
  await get('refreshBtn').listeners.click();
  assert.equal(get('accountsEnabled').checked, true);
  const list = get('accountsList').innerHTML;
  assert.equal((list.match(/category-drag-handle/g) || []).length, 3);
  assert.doesNotMatch(list, /Старая/);
  const archive = get('accountsArchive').innerHTML;
  assert.match(archive, /<details class="accounts-archive" open><summary>Архив \(2\) ⚠️<\/summary>/);
  assert.match(archive, /в архиве/);
  assert.doesNotMatch(archive, /category-drag-handle/);
  assert.equal((archive.match(/data-restore-account/g) || []).length, 2);
  // Only an account without operations can be deleted for good.
  assert.match(archive, /data-delete-account="5"/);
  assert.doesNotMatch(archive, /data-delete-account="4"/);
  assert.match(archive, /На архивном счёте не ноль[\s\S]*Верните счёт или исправьте остаток/);
});

test('expense form: the spending envelope card by default, no credit card, a backdated hint', async () => {
  const {get, requests} = setup();
  await get('refreshBtn').listeners.click();
  get('quickExpenseBtn').listeners.click();
  assert.equal(get('expenseAccountField').classList.contains('hidden'), false);
  const options = get('expenseAccount').innerHTML;
  assert.match(options, /Карта 1/);
  assert.doesNotMatch(options, /Кредитка|Старая/);
  assert.equal(get('expenseAccount').value, '2');
  get('expenseDate').value = '2026-10-05';
  get('expenseDate').listeners.change();
  assert.equal(get('expenseAccountHint').classList.contains('hidden'), false);
  assert.match(get('expenseAccountHint').textContent, /не изменит/);
  get('expenseAmount').value = '2 000';
  get('expenseCategory').value = '7';
  await get('expenseForm').listeners.submit({preventDefault(){}, target: get('expenseForm')});
  assert.equal(body(requests, '/api/transactions').account_id, 2);
});

test('editing an old expense on an archived account keeps it selectable', async () => {
  const {get, sandbox, responses} = setup();
  responses['/api/transactions'] = [{id: 9, type: 'expense', amount: 10, tx_date: '2026-10-01', category_id: 7, account_id: 4, account_title: 'Старая', account_emoji: '🗂', account_archived: 1}];
  await get('refreshBtn').listeners.click();
  assert.match(get('transactionsList').innerHTML, /🗂 Старая \(архив\)/);
  sandbox.window.editTransaction(9);
  assert.match(get('expenseAccount').innerHTML, /Старая \(архив\)/);
  assert.equal(get('expenseAccount').value, '4');
});

test('switching on without accounts opens the wizard; balances come from the envelopes', async () => {
  const {get, requests, responses, sandbox} = setup({...ACCOUNTS, enabled: false, accounts: [], main_account_id: null,
    envelopes: ACCOUNTS.envelopes.map(e => ({...e, account_id: null}))});
  await get('refreshBtn').listeners.click();
  get('accountsEnabled').checked = true;
  await get('accountsEnabled').listeners.change();
  assert.equal(get('accountsEnabled').checked, false);
  assert.equal(get('accountsWizard').open, true);
  assert.equal(requests.filter(r => r.url === '/api/accounts/settings').length, 0);

  // Step 1: two cards and a credit card.
  get('wizTitle-0').value = 'Карта 1';
  await get('accountsWizardBody').listeners.click({target: {id: 'wizAddRow'}});
  get('wizTitle-1').value = 'Карта 2';
  await get('accountsWizardBody').listeners.click({target: {id: 'wizAddRow'}});
  get('wizTitle-2').value = 'Кредитка';
  get('wizKind-2').value = 'credit';
  await get('accountsWizardBody').listeners.change({target: {dataset: {wizKind: '2'}}});
  assert.match(get('accountsWizardBody').innerHTML, /wizLimit-2/);
  get('wizLimit-2').value = '50 000';
  await get('accountsWizardNext').listeners.click();

  // Step 2: the credit card is never offered for the main account or an envelope.
  const step2 = get('accountsWizardBody').innerHTML;
  assert.match(step2, /Основной счёт/);
  assert.doesNotMatch(step2, /<option value="2">/);
  get('wizMain').value = '0';
  get('wizEnvelope-spending').value = '1';
  get('wizEnvelope-buffer').value = '0';
  get('wizEnvelope-piggy').value = '1';
  await get('accountsWizardNext').listeners.click();

  // Step 3: each card is prefilled with the sum of its envelopes.
  const step3 = get('accountsWizardBody').innerHTML;
  assert.match(step3, /id="wizBalance-0"[^>]*value="30.000,00"/);
  assert.match(step3, /id="wizBalance-1"[^>]*value="105.000,00"/);
  assert.match(step3, /Кредитка · долг сейчас<input id="wizBalance-2"[^>]*value="0,00"/);
  get('wizBalance-0').value = '30000';
  get('wizBalance-1').value = '105 500';
  get('wizBalance-2').value = '0';
  responses['/api/accounts'] = ACCOUNTS;
  await get('accountsWizardNext').listeners.click();
  const payload = body(requests, '/api/accounts/setup');
  assert.deepEqual(payload.accounts.map(a => [a.title, a.kind, a.balance, a.is_main]), [
    ['Карта 1', 'debit', 30000, true], ['Карта 2', 'debit', 105500, false], ['Кредитка', 'credit', 0, false],
  ]);
  assert.equal(payload.accounts[2].credit_limit, 50000);
  assert.deepEqual(payload.envelopes, {spending: 1, buffer: 0, piggy: 1});
});

test('switching on with accounts already added enables directly and reports missed operations', async () => {
  const {get, requests, responses, sandbox} = setup({...ACCOUNTS, enabled: false});
  let message = '';
  sandbox.alert = text => { message = text; };
  responses['/api/accounts/settings'] = {...ACCOUNTS, operations_without_account: 3};
  await get('refreshBtn').listeners.click();
  get('accountsEnabled').checked = true;
  await get('accountsEnabled').listeners.change();
  assert.deepEqual(body(requests, '/api/accounts/settings'), {enabled: true});
  assert.match(message, /без карты: 3/);
});

test('accounts are reordered by dragging the ☰ handle, like the categories', async () => {
  const {get, requests} = setup();
  await get('refreshBtn').listeners.click();
  const list = get('accountsList');
  const order = [1, 2, 3].map((id, index) => {
    const item = element();
    item.dataset.accountId = String(id);
    item.offsetTop = index * 50;
    item.offsetHeight = 40;
    item.closest = sel => (sel === '.account-item' ? item : null);
    return item;
  });
  let items = [...order];
  const sync = () => items.forEach((item, index) => {
    item.offsetTop = index * 50;
    item.nextElementSibling = items[index + 1] || null;
  });
  sync();
  list.getBoundingClientRect = () => ({top: 0});
  list.querySelectorAll = selector => selector.includes(':not(.dragging)')
    ? items.filter(item => !item.classList.contains('dragging')) : items;
  list.insertBefore = (item, before) => {
    items = items.filter(other => other !== item);
    items.splice(before ? items.indexOf(before) : items.length, 0, item);
    sync();
  };
  const handle = {closest: sel => (sel === '.category-drag-handle' ? {closest: () => order[0]} : null)};
  list.listeners.pointerdown({pointerType: 'mouse', pointerId: 1, target: handle, clientY: 10, preventDefault() {}});
  list.listeners.pointermove({pointerId: 1, clientY: 120, preventDefault() {}});
  await list.listeners.pointerup({pointerId: 1});
  assert.deepEqual(body(requests, '/api/accounts/order'), {account_ids: [2, 3, 1]});
});

test('archive and restore buttons call their endpoints', async () => {
  const {get, requests} = setup();
  await get('refreshBtn').listeners.click();
  await get('accountsArchive').listeners.click({target: {closest: sel => (sel === '[data-restore-account]' ? {dataset: {restoreAccount: '4'}} : null)}});
  assert.ok(requests.some(r => r.url === '/api/accounts/4/restore' && r.method === 'POST'));
  get('accountsList').listeners.click({target: {closest: sel => (sel === '[data-edit-account]' ? {dataset: {editAccount: '2'}} : null)}});
  assert.equal(get('accountDialog').open, true);
  assert.equal(get('accountTitle').value, 'Карта 2');
  await get('archiveAccountBtn').listeners.click();
  assert.ok(requests.some(r => r.url === '/api/accounts/2/archive' && r.method === 'POST'));
});
