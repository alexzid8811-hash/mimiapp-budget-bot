const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup() {
  const elements = new Map(), requests = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, textContent: '', innerHTML: '', style: {}, listeners: {},
      classList: {add() {}, remove() {}, toggle() {}},
      addEventListener(type, fn) { this.listeners[type] = fn; },
      showModal() {}, close() {},
    });
    return elements.get(id);
  };
  const responses = {
    '/api/bootstrap': { user: {}, budget_timezone: 'Europe/Moscow', settings: {currency:'RUB'}, categories: [], income_rules: [], bill_rules: [] },
    '/api/payroll-settings': {settings: {}}, '/api/vacations': [], '/api/transactions': [], '/api/plan': [],
    '/api/dashboard': {daily_available: 999, period: {start:'2026-09-07',end:'2026-09-21',days_left:7}, reserve: {}, forecast: []},
    '/api/cashflow-settings': {cashflow_enabled:1,start_date:'2026-09-01'},
    '/api/cashflow': {enabled:true,period_budget:100,available_today:5.25,remaining_period:50,buffer_balance:40,current_cash:45.25,periods:[],horizon_end:'2026-10-15'},
    '/api/piggy-bank': {balance:0,movements:[]},
  };
  const sandbox = {
    console, URLSearchParams, Intl, Date, Number, JSON,
    setTimeout: () => 0, setInterval: () => 0, clearInterval() {},
    window: {location:{hash:'',search:''},addEventListener(){}},
    document: {getElementById:get, querySelectorAll:()=>[],querySelector:()=>null},
    fetch: async (url, options) => {
      requests.push({url,body:options?.body});
      return {ok:true,status:200,json:async()=>responses[url] ?? {}};
    },
  };
  vm.createContext(sandbox);
  for (const name of ['budget-date.js','app.js','cashflow-ui.js','buffer-ui.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname,'../app/static',name),'utf8'),sandbox);
  }
  return {sandbox, get, requests, responses};
}

test('refresh renders the safe calculation and buffer, without changing algorithms', async () => {
  const {get, requests} = setup();
  await get('refreshBtn').listeners.click();
  assert.equal(requests.filter(r=>r.url==='/api/cashflow').length,1);
  assert.match(get('dailyAvailable').textContent,/5,25/);
  assert.match(get('bufferPageDaily').textContent,/5,25/);
  assert.doesNotMatch(get('dailyAvailable').textContent,/999/);
  await get('refreshBtn').listeners.click();
  assert.match(get('dailyAvailable').textContent,/5,25/);
});

test('editing manual salary preserves kind, payday flag and active state', async () => {
  const {sandbox,get,requests} = setup();
  vm.runInContext("openRule('income',{id:7,title:'Зарплата',amount:10000,day_of_month:7,kind:'salary',is_payday:true,active:0})",sandbox);
  await get('ruleForm').listeners.submit({preventDefault(){}});
  const body = JSON.parse(requests[0].body);
  assert.equal(body.kind,'salary');
  assert.equal(body.is_payday,true);
  assert.equal(body.active,false);
  assert.match(body.effective_date,/^\d{4}-\d{2}-\d{2}$/);
});

test('disabled cashflow intentionally displays legacy calculation', async () => {
  const {get,responses} = setup();
  responses['/api/cashflow'] = {enabled:false,settings:{}};
  responses['/api/cashflow-settings'].cashflow_enabled = 0;
  await get('refreshBtn').listeners.click();
  assert.match(get('dailyAvailable').textContent,/999/);
  assert.equal(get('bufferPageDaily').textContent,'—');
});

test('budget dates follow configured timezone, including local midnight', () => {
  const {sandbox} = setup();
  const helper = sandbox.window.budgetDate;
  assert.equal(helper.today(new Date('2026-09-14T21:30:00Z')),'2026-09-15');
  helper.configure('Asia/Vladivostok');
  assert.equal(helper.today(new Date('2026-09-14T15:00:00Z')),'2026-09-15');
  assert.equal(helper.shift('2026-03-01',-1),'2026-02-28');
});

test('older reload responses cannot overwrite newer values', async () => {
  const {sandbox,get,responses} = setup();
  const initialFetch=sandbox.fetch;
  let release;
  let first=true;
  sandbox.fetch=async(url, options)=>{
    if(url==='/api/cashflow' && first){
      first=false;
      const old={...responses[url],available_today:777};
      return new Promise(resolve=>{release=()=>resolve({ok:true,status:200,json:async()=>old});});
    }
    return initialFetch(url,options);
  };
  const oldLoad=get('refreshBtn').listeners.click();
  await get('refreshBtn').listeners.click();
  release();
  await oldLoad;
  assert.match(get('dailyAvailable').textContent,/5,25/);
});

test('paid bill can be edited and its remainder sent to piggy bank', async () => {
  const {sandbox,get,requests,responses} = setup();
  responses['/api/plan'] = [{
    id: 3, payment_id: 17, title: 'Коммуналка', due_date: '2026-09-10',
    amount: 10000, planned_amount: 10000, paid: true,
    remainder_destination: 'budget', remainder_amount: 0,
  }];
  await get('refreshBtn').listeners.click();
  assert.match(get('planList').innerHTML,/Изменить/);

  sandbox.window.editBillPayment(17);
  assert.equal(get('billPaymentAmount').value,10000);
  assert.equal(get('billRemainderDestination').value,'budget');
  get('billPaymentAmount').value='8000';
  get('billRemainderDestination').value='piggy';
  await get('billPaymentForm').listeners.submit({preventDefault(){}});

  const request = requests.find(r => r.url === '/api/bill-payments/17');
  assert.deepEqual(JSON.parse(request.body), {amount:8000,remainder_destination:'piggy'});
});
