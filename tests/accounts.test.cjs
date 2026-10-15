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

// A click on an element matching ``selector`` with the given data attributes.
const hit = (selector, dataset) => ({target: {closest: sel => (sel === selector ? {dataset} : null)}});
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

test('an account opens its history with transfer, expense and correction actions', async () => {
  const {get, requests, responses} = setup();
  responses['/api/accounts/1/history'] = {account: ACCOUNTS.accounts[0], items: [
    {type: 'transfer', id: 5, date: '2026-10-12', amount: -10000, title: 'На 🏦 Карта 2', sub: 'Перевод', emoji: '↔️', counted: true, deletable: true},
    {type: 'expense', id: 3, date: '2026-09-30', amount: -50, title: 'Кофе', sub: '', emoji: '☕', counted: false},
  ]};
  await get('refreshBtn').listeners.click();
  await get('accList').listeners.click(hit('[data-account-id]', {accountId: '1'}));
  assert.equal(get('accountHistoryDialog').open, true);
  assert.match(get('accountHistoryBalance').textContent, /55.000,00/);
  assert.match(get('accountHistoryActions').innerHTML, /Перевести[\s\S]*Записать трату с этого счёта[\s\S]*Исправить остаток/);
  const list = get('accountHistoryList').innerHTML;
  assert.match(list, /На 🏦 Карта 2[\s\S]*−10.000,00/);
  assert.match(list, /до начала учёта, не в остатке/);
  assert.match(list, /data-history-delete="transfer:5"/);

  // «Перевести» opens the transfer form from this account.
  get('accountHistoryActions').listeners.click(hit('[data-history-action]', {historyAction: 'transfer'}));
  assert.equal(get('transferDialog').open, true);
  assert.equal(get('transferFrom').value, '1');
  assert.doesNotMatch(get('transferTo').innerHTML, /Кредитка/);
  get('transferTo').value = '2';
  get('transferAmount').value = '10 000';
  get('transferDate').value = '2026-10-15';
  await get('transferForm').listeners.submit({preventDefault() {}});
  assert.deepEqual(body(requests, '/api/accounts/transfers'), {from_account_id: 1, to_account_id: 2, amount: 10000, date: '2026-10-15', note: '', kind: 'manual'});

  // «Исправить остаток» sends the real amount.
  get('accountHistoryActions').listeners.click(hit('[data-history-action]', {historyAction: 'adjust'}));
  assert.equal(get('adjustDialog').open, true);
  assert.match(get('adjustTarget').value, /55.000,00/);
  get('adjustTarget').value = '55 500';
  await get('adjustForm').listeners.submit({preventDefault() {}});
  assert.equal(body(requests, '/api/accounts/1/adjust').target, 55500);
});

test('a credit card history offers to correct the debt', async () => {
  const {get, responses} = setup();
  responses['/api/accounts/3/history'] = {account: ACCOUNTS.accounts[2], items: []};
  await get('refreshBtn').listeners.click();
  await get('accCredit').listeners.click(hit('[data-account-id]', {accountId: '3'}));
  assert.equal(get('accountHistoryCaption').textContent, 'Долг');
  assert.match(get('accountHistoryNote').textContent, /Доступно 50.000,00/);
  assert.match(get('accountHistoryActions').innerHTML, /Исправить долг/);
  assert.doesNotMatch(get('accountHistoryActions').innerHTML, /Перевести/);
});

test('income forms take the main card by default and send it', async () => {
  const {get, requests} = setup();
  await get('refreshBtn').listeners.click();
  get('quickIncomeBtn').listeners.click();
  assert.equal(get('incomeTxAccountField').classList.contains('hidden'), false);
  assert.equal(get('incomeTxAccount').value, '1');
  assert.doesNotMatch(get('incomeTxAccount').innerHTML, /Кредитка/);
  get('incomeTxAccount').value = '2';
  get('incomeTxAmount').value = '500';
  await get('incomeTxForm').listeners.submit({preventDefault() {}, target: get('incomeTxForm')});
  assert.equal(body(requests, '/api/transactions').account_id, 2);
});

test('a payday counted without confirmation is offered in «Нужно перевести»', async () => {
  const {get, requests} = setup({...ACCOUNTS, hints: [
    {type: 'payroll_income', period_start: '2026-09-23', payday: '2026-09-22', title: 'Аванс', amount: 25000, confirmed: false, account_id: 1},
  ]});
  await get('refreshBtn').listeners.click();
  const hints = get('accHints').innerHTML;
  assert.match(hints, /Нужно перевести \(1\)/);
  assert.match(hints, /Аванс 22\.09 не отмечена полученной — записать приход 25.000,00.₽ на 💳 Карта 1/);
  await get('accHints').listeners.click(hit('[data-hint-income]', {hintIncome: '2026-09-23'}));
  assert.deepEqual(body(requests, '/api/accounts/payroll-income'), {period_start: '2026-09-23'});
});

test('confirming a payout lets the user pick the card', async () => {
  const {get, sandbox, requests, responses} = setup();
  responses['/api/cashflow'].periods = [
    {start:'2026-10-07',end:'2026-10-21',kind:'Зарплата',days:15,payday:'2026-10-07',received:61199.72,received_editable:true,income_pending:true,income_confirmed:false,override_key:'2026-10-08',payday_amount:61199.72,planned_payday_amount:61199.72,mandatory:0,free:0,daily:1,put_aside:0,take:0,buffer:0,to_card:0},
  ];
  await get('refreshBtn').listeners.click();
  sandbox.window.confirmCashflowIncome('2026-10-08');
  assert.equal(get('cashflowIncomeAccountField').classList.contains('hidden'), false);
  assert.equal(get('cashflowIncomeAccount').value, '1');
  get('cashflowIncomeAccount').value = '2';
  await get('cashflowIncomeForm').listeners.submit({preventDefault() {}});
  assert.deepEqual(body(requests, '/api/cashflow/income-overrides/2026-10-08'), {amount: 61199.72, confirmed: true, account_id: 2});
  // Editing the amount does not ask for a card.
  sandbox.window.editCashflowIncome('2026-10-08');
  assert.equal(get('cashflowIncomeAccountField').classList.contains('hidden'), true);
});

const SPLIT = (() => {
  const data = JSON.parse(JSON.stringify(ACCOUNTS));
  data.accounts[0].breakdown = {envelopes: [{key: 'buffer', label: 'буфер', amount: 30000}], envelopes_total: 30000, outgoing_hints: 0, difference: 25000};
  data.accounts[1].breakdown = {envelopes: [{key: 'spending', label: 'на траты', amount: 25000}, {key: 'piggy', label: 'копилка', amount: 80000}], envelopes_total: 105000, outgoing_hints: 0, difference: -5000};
  data.accounts[0].envelopes = ['buffer'];
  data.hints = [{type: 'transfer', from_account_id: 1, to_account_id: 2, amount: 15000}];
  return data;
})();

test('home: each card shows how its money divides between envelopes and what is off', async () => {
  const {get} = setup(SPLIT);
  await get('refreshBtn').listeners.click();
  const list = get('accList').innerHTML;
  assert.match(list, /<div class="acc-split num">буфер 30.000<\/div>/);
  assert.match(list, /на траты 25.000 · копилка 80.000/);
  assert.match(list, /вне бюджета 25.000,00.₽/);
  // No envelope to add to on card 1 (only the buffer lies there).
  assert.doesNotMatch(list, /data-add-to/);
  assert.match(list, /не хватает 5.000,00.₽[\s\S]*data-transfer-to="2"[\s\S]*Исправить остаток/);
  assert.doesNotMatch(get('accCredit').innerHTML, /acc-split/);
});

test('«вне бюджета» on a card with the piggy bank adds it there without moving money', async () => {
  const data = JSON.parse(JSON.stringify(SPLIT));
  data.accounts[1].breakdown.difference = 500;
  const {get, requests} = setup(data);
  await get('refreshBtn').listeners.click();
  assert.match(get('accList').innerHTML, /data-add-to="piggy" data-amount="500">Добавить в копилку/);
  await get('accList').listeners.click(hit('[data-add-to]', {addTo: 'piggy', amount: '500'}));
  const sent = body(requests, '/api/piggy-bank/deposit');
  assert.equal(sent.amount, 500);
  assert.equal(sent.account_move, false);
});

test('«не хватает» offers a transfer from the card with money outside the budget', async () => {
  const {get} = setup(SPLIT);
  await get('refreshBtn').listeners.click();
  get('accList').listeners.click(hit('[data-transfer-to]', {transferTo: '2', amount: '5000'}));
  assert.equal(get('transferDialog').open, true);
  assert.equal(get('transferFrom').value, '1');
  assert.equal(get('transferTo').value, '2');
  assert.match(get('transferAmount').value, /5.000,00/);
});

test('«Нужно перевести» lists the transfer and «Перевёл» records it', async () => {
  const {get, requests} = setup(SPLIT);
  await get('refreshBtn').listeners.click();
  assert.match(get('accHints').innerHTML, /Нужно перевести \(1\)[\s\S]*Переведи 15.000,00.₽ с 💳 Карта 1 на 🏦 Карта 2[\s\S]*Перевёл/);
  await get('accHints').listeners.click(hit('[data-hint-transfer]', {hintTransfer: '1:2:15000'}));
  const sent = body(requests, '/api/accounts/transfers');
  assert.deepEqual([sent.from_account_id, sent.to_account_id, sent.amount, sent.kind], [1, 2, 15000, 'hint']);
});

test('«Где лежат деньги»: every envelope on a non-credit card, moving one offers to move its money', async () => {
  const {get, requests} = setup();
  await get('refreshBtn').listeners.click();
  assert.equal(get('envelopePlacesBlock').classList.contains('hidden'), false);
  const places = get('envelopePlaces').innerHTML;
  assert.match(places, /на траты · 25.000,00.₽<select id="envelopePlace-spending" data-envelope="spending">/);
  assert.match(places, /копилка/);
  assert.doesNotMatch(places, /Кредитка|Старая/);
  await get('envelopePlaces').listeners.change({target: {dataset: {envelope: 'buffer'}, value: '2'}});
  assert.deepEqual(body(requests, '/api/accounts/envelopes/buffer'), {account_id: 2, move_money: true});
});

test('«Из чего»: the default follows the envelopes on the chosen card', async () => {
  const data = JSON.parse(JSON.stringify(ACCOUNTS));
  data.mode = 'irregular';
  data.envelopes = [
    {key: 'spending', label: 'свободные', account_id: 1, amount: 1},
    {key: 'buffer', label: 'на обязательные', account_id: 1, amount: 1},
    {key: 'piggy', label: 'копилка', account_id: 2, amount: 1},
    {key: 'reserve', label: 'резерв', account_id: 6, amount: 1},
  ];
  data.accounts.push({id: 6, title: 'Резервный', emoji: '🛟', kind: 'savings', balance: 1, is_main: false, archived: false, opening_date: '2026-10-01', envelopes: ['reserve']});
  data.accounts.push({id: 7, title: 'Общий', emoji: '🧺', kind: 'savings', balance: 1, is_main: false, archived: false, opening_date: '2026-10-01', envelopes: []});
  const {get, requests} = setup(data);
  await get('refreshBtn').listeners.click();
  get('quickExpenseBtn').listeners.click();
  const choose = id => { get('expenseAccount').value = String(id); get('expenseAccount').listeners.change(); return get('expenseFunding'); };
  assert.equal(get('expenseAccount').value, '1');
  assert.equal(get('expenseFunding').value, 'spending');
  assert.doesNotMatch(get('expenseFunding').innerHTML, /Из копилки|Из резерва/);
  assert.equal(choose(2).value, 'piggy');
  assert.match(get('expenseFunding').innerHTML, /Из бюджета на траты[\s\S]*Из копилки[\s\S]*Вне бюджета/);
  assert.equal(choose(6).value, 'reserve');
  assert.equal(choose(7).value, 'none');
  // Piggy bank and reserve on one card: the choice is required.
  data.envelopes[3].account_id = 2;
  const again = setup(data);
  await again.get('refreshBtn').listeners.click();
  again.get('quickExpenseBtn').listeners.click();
  again.get('expenseAccount').value = '2';
  again.get('expenseAccount').listeners.change();
  assert.equal(again.get('expenseFunding').value, '');
  again.get('expenseAmount').value = '100';
  again.get('expenseCategory').value = '7';
  await again.get('expenseForm').listeners.submit({preventDefault() {}, target: again.get('expenseForm')});
  assert.equal(again.requests.filter(r => r.url === '/api/transactions' && r.method === 'POST').length, 0);
  again.get('expenseFunding').value = 'reserve';
  await again.get('expenseForm').listeners.submit({preventDefault() {}, target: again.get('expenseForm')});
  const sent = body(again.requests, '/api/transactions');
  assert.deepEqual([sent.account_id, sent.funding_envelope], [2, 'reserve']);
  assert.equal(requests.length > 0, true);
});

test('piggy bank: outside money comes from outside or a card; a withdrawal goes to a card or outside', async () => {
  const {get, requests} = setup();
  await get('refreshBtn').listeners.click();
  get('piggyDepositBtn').listeners.click();
  assert.equal(get('piggyAccountField').classList.contains('hidden'), false);
  assert.equal(get('piggyAccountLabel').textContent, 'Откуда');
  assert.match(get('piggyAccount').innerHTML, /<option value="">Извне — не с моих карт<\/option>/);
  assert.equal(get('piggyAccount').value, '');
  get('piggyAmount').value = '100';
  await get('piggyForm').listeners.submit({preventDefault() {}});
  assert.equal(body(requests, '/api/piggy-bank/deposit').account_id, null);
  get('piggyWithdrawBtn').listeners.click();
  assert.equal(get('piggyAccountLabel').textContent, 'Куда');
  assert.equal(get('piggyAccount').value, '2');
  get('piggyAmount').value = '50';
  await get('piggyForm').listeners.submit({preventDefault() {}});
  assert.equal(body(requests, '/api/piggy-bank/withdraw').account_id, 2);
  // The day's remainder moves between the envelopes' cards on its own.
  get('piggyTransferBtn').listeners.click();
  assert.equal(get('piggyAccountField').classList.contains('hidden'), true);
});
