const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const sample = {
  mode: 'month', label: 'Сентябрь 2026', start: '2026-09-01', end: '2026-09-30', today: '2026-09-15',
  previous_anchor: '2026-08-01', next_anchor: null, previous_partial: true,
  totals: {daily: 1000, bills: 3000, piggy: 1000, total: 5000},
  previous_totals: {daily: 800, bills: 3000, piggy: 200, total: 4000},
  categories: [
    {id: 2, title: 'Кафе', emoji: '☕', amount: 600, previous: 300, share: 0.6, count: 4,
     top: [{date: '2026-09-12', amount: 400, note: '<b>ужин</b>'}]},
    {id: 1, title: 'Продукты', emoji: '🛒', amount: 400, previous: 0, share: 0.4, count: 1, top: []},
  ],
  days: [{date: '2026-09-01', amount: 100}, {date: '2026-09-02', amount: 0}],
  daily_limit: null,
  months: [{month: '2026-08', label: 'Август', daily: 800, bills: 3000, piggy: 200},
           {month: '2026-09', label: 'Сентябрь', daily: 1000, bills: 3000, piggy: 1000}],
  bills: [
    {title: 'Аренда', paid_amount: 3000, due_amount: 0, count: 1, paid_count: 1, next_due: null},
    {title: 'Связь', paid_amount: 0, due_amount: 450, count: 1, paid_count: 0, next_due: '2026-09-30', next_amount: 450},
    {title: 'Авто', paid_amount: 0, due_amount: 85500, count: 3, paid_count: 0, next_due: '2026-10-08', next_amount: 28500},
    {title: 'Ипотека', paid_amount: 25000, due_amount: 75000, count: 4, paid_count: 1, next_due: '2026-10-24', next_amount: 25000},
    {title: 'Интернет', paid_amount: 0, due_amount: 0, count: 0, paid_count: 0, next_due: '2026-10-03', next_amount: 800},
  ],
};

function setup() {
  const elements = new Map(), requests = [];
  const element = () => ({
    innerHTML: '', textContent: '', disabled: false, className: '', style: {}, dataset: {}, listeners: {},
    classList: {toggle() {}, add() {}, remove() {}, contains: () => false},
    addEventListener(type, fn) { this.listeners[type] = fn; },
    setAttribute() {},
    querySelector() { return element(); },
    getBoundingClientRect: () => ({width: 380}),
  });
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const modes = ['period', 'month', 'year'].map(mode => ({...element(), dataset: {amode: mode}}));
  const sandbox = {
    console, URLSearchParams, Intl, Date, Math, Number, String,
    window: {},
    state: {bootstrap: {categories: [{id: 1}, {id: 2}]}},
    api: async url => { requests.push(url); return sample; },
    document: {
      getElementById: get,
      querySelectorAll: sel => sel === '[data-amode]' ? modes : [],
      querySelector: () => null,
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/analytics-ui.js'), 'utf8'), sandbox);
  return {sandbox, get, modes, requests};
}

test('analytics page renders totals, categories and bills', async () => {
  const {sandbox, get, requests} = setup();
  await sandbox.window.budgetAnalytics.load();
  assert.equal(requests[0], '/api/analytics?mode=month');
  assert.equal(get('analyticsLabel').textContent, 'Сентябрь 2026');
  assert.match(get('analyticsTotal').textContent, /5[\s ]000/);
  assert.match(get('analyticsDelta').textContent, /▲ 25% к этому дню прошлого месяца/);
  assert.equal(get('analyticsNext').disabled, true);

  const cats = get('analyticsCategories').innerHTML;
  assert.match(cats, /Кафе[\s\S]*Продукты/);
  assert.match(cats, /▲ 100%/);
  assert.match(cats, /новая/);
  assert.match(cats, /Изменение к этому дню прошлого месяца/);
  // Colour follows the category's position in the settings list, not its rank.
  assert.match(cats, /Кафе[\s\S]*?var\(--cat-2\)/);

  const bills = get('analyticsBills').innerHTML;
  assert.match(bills, /Аренда<small class="ok">✓ оплачено<\/small>/);
  assert.match(bills, /Связь<small>к оплате 30 сент\.<\/small>/);
  // Several unpaid months: the row says how many and names the nearest payment,
  // and the amount column is the whole period for every row.
  assert.match(bills, /Авто<small>ещё 3 платежа, ближайший 8 окт\. — 28[\s ]500[\s ]₽<\/small><\/span><span class="num">85[\s ]500/);
  assert.match(bills, /Ипотека<small>оплачено 25[\s ]000[\s ]₽ \(1 из 4\) · ещё 3 платежа, ближайший 24 окт\. — 25[\s ]000[\s ]₽<\/small><\/span><span class="num">100[\s ]000/);
  assert.match(get('analyticsBillsTotal').textContent, /оплачено 28[\s ]000[\s ]₽ из 188[\s ]950/);
  assert.match(bills, /Ещё к оплате<\/span><span class="num">160[\s ]950/);
  // A bill that starts after the period is listed but not summed.
  assert.match(bills, /Интернет<small>первый платёж 3 окт\. — 800[\s\u00a0]₽<\/small><\/span><span class="num muted">—/);
  assert.equal(get('analyticsDaysNote').textContent, '');
  assert.equal(get('analyticsDaysLegend').innerHTML.match(/в среднем/gi).length, 1);
});

test('switching mode and expanding a category', async () => {
  const {sandbox, get, modes, requests} = setup();
  await sandbox.window.budgetAnalytics.load();
  get('analyticsCategories').listeners.click({target: {closest: () => ({dataset: {cat: '2'}})}});
  const html = get('analyticsCategories').innerHTML;
  assert.match(html, /&lt;b&gt;ужин&lt;\/b&gt;/);
  assert.match(html, /ещё 3 опер\./);

  await modes[2].listeners.click();
  assert.equal(requests.at(-1), '/api/analytics?mode=year');
  await get('analyticsPrev').listeners.click();
  assert.equal(requests.at(-1), '/api/analytics?mode=year&anchor=2026-08-01');
});
