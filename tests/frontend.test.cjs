const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup() {
  const elements = new Map(), requests = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', checked: false, textContent: '', innerHTML: '', style: {}, listeners: {}, dataset: {},
      setAttribute() {}, removeAttribute() {}, replaceChildren() {}, appendChild() {},
      reset() {},
      classList: (() => {
        const classes = new Set();
        return {
          add: (...names) => names.forEach(n => classes.add(n)),
          remove: (...names) => names.forEach(n => classes.delete(n)),
          toggle: (name, force) => {
            const on = force === undefined ? !classes.has(name) : Boolean(force);
            if (on) classes.add(name); else classes.delete(name);
            return on;
          },
          contains: name => classes.has(name),
        };
      })(),
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
    '/api/cashflow': {enabled:true,period_budget:100,available_today:5.25,remaining_period:50,buffer_balance:40,current_cash:45.25,periods:[],horizon_end:'2026-10-15',overspend:0,today_target:5.25,period:{start:'2026-09-07',end:'2026-09-21',payday:'2026-09-21',payday_kind:'Выплата'}},
    '/api/piggy-bank': {balance:0,movements:[]},
  };
  const sandbox = {
    console, URLSearchParams, Intl, Date, Number, JSON,
    setTimeout: () => 0, setInterval: () => 0, clearInterval() {},
    window: {location:{hash:'',search:''},addEventListener(){}},
    document: {addEventListener(){}, body:{classList:{toggle(){}}}, createElement:()=>({}),getElementById:get, querySelectorAll:()=>[],querySelector:()=>null},
    fetch: async (url, options) => {
      requests.push({url,body:options?.body});
      return {ok:true,status:200,json:async()=>responses[url] ?? {}};
    },
  };
  vm.createContext(sandbox);
  for (const name of ['budget-date.js','money-input.js','app.js','cashflow-ui.js','buffer-ui.js','design-ui.js']) {
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

test('paid bill card keeps title, amount and paid total in horizontal rows', () => {
  const {sandbox,get} = setup();
  vm.runInContext(`state.plan=[{
    id:11,title:'интернет',amount:0,planned_amount:1400,due_date:'2026-09-17',
    paid:true,payment_id:19,category_emoji:'📌',remainder_amount:0
  }]; renderPlan();`, sandbox);
  const html = get('planList').innerHTML;
  assert.match(html, /bill-heading/);
  assert.match(html, /интернет<\/div><div class="amount expense">0,00/);
  assert.match(html, /17 сент\. · уже учтено в бюджете/);
  assert.match(html, /bill-paid-caption">Оплачено из 1[\s\u00a0]400,00[\s\u00a0]₽<\/div>/);
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

test('paid bill can be edited from transaction history and its remainder sent to piggy bank', async () => {
  const {sandbox,get,requests,responses} = setup();
  responses['/api/transactions'] = [{
    id: 17, bill_rule_id: 3, bill_title: 'Коммуналка', tx_date: '2026-08-10',
    type: 'expense', amount: 10000, bill_planned_amount: 10000,
    remainder_destination: 'budget', remainder_amount: 0,
  }];
  await get('refreshBtn').listeners.click();
  assert.match(get('transactionsList').innerHTML,/Изменить/);

  sandbox.window.editBillPayment(17);
  assert.equal(get('billPaymentAmount').value,10000);
  assert.equal(get('billRemainderDestination').value,'budget');
  get('billPaymentAmount').value='8000';
  get('billRemainderDestination').value='piggy';
  await get('billPaymentForm').listeners.submit({preventDefault(){}});

  const request = requests.find(r => r.url === '/api/bill-payments/17');
  assert.deepEqual(JSON.parse(request.body), {amount:8000,remainder_destination:'piggy'});
});

test('piggy bank remains accessible through reserves tabs', () => {
  const html = fs.readFileSync(path.join(__dirname,'../app/static/index.html'),'utf8');
  const styles = fs.readFileSync(path.join(__dirname,'../app/static/styles.css'),'utf8');
  const bufferStart = html.indexOf('data-page="buffer"');
  const piggyStart = html.indexOf('data-page="piggy"');
  const settingsStart = html.indexOf('data-page="settings"');
  assert.ok(bufferStart >= 0 && piggyStart > bufferStart && settingsStart > piggyStart);
  assert.match(html,/data-seg="piggy"/);
  assert.match(styles,/grid-template-columns:repeat\(5,1fr\)/);
});

test('legacy payroll correction section is removed', () => {
  const html = fs.readFileSync(path.join(__dirname,'../app/static/index.html'),'utf8');
  assert.doesNotMatch(html,/Исправить расчёт за текущий или прошлый период/);
  assert.doesNotMatch(html,/payrollEffectiveDate/);
});

test('category order uses a touch-friendly drag handle', async () => {
  const {get,responses}=setup();
  responses['/api/bootstrap'].categories=[
    {id:1,title:'Продукты',emoji:'🛒'},
    {id:2,title:'Транспорт',emoji:'🚕'},
    {id:3,title:'Обед',emoji:'🍲'},
  ];
  await get('refreshBtn').listeners.click();
  assert.match(get('categoriesList').innerHTML,/category-drag-handle/);
  assert.match(get('categoriesList').innerHTML,/data-category-id="3"/);
  assert.doesNotMatch(get('categoriesList').innerHTML,/category-move/);
  const script = fs.readFileSync(path.join(__dirname,'../app/static/app.js'),'utf8');
  assert.match(script,/pointermove/);
  assert.match(script,/touchstart/);
  assert.match(script,/touchmove/);
  assert.match(script,/passive:false/);
  assert.match(script,/\/api\/categories\/order/);
});

test('buffer has no separate vacation reserve card', () => {
  const html = fs.readFileSync(path.join(__dirname,'../app/static/index.html'),'utf8');
  assert.doesNotMatch(html,/Отложено из отпускных|vacationReserve/);
});

test('layout expands responsively on tablets and desktop screens', () => {
  const html = fs.readFileSync(path.join(__dirname,'../app/static/index.html'),'utf8');
  const styles = fs.readFileSync(path.join(__dirname,'../app/static/styles.css'),'utf8');
  assert.match(html,/class="card home-buffer"/);
  assert.match(html,/class="card home-forecast"/);
  assert.match(styles,/#app\{width:100%;max-width:none/);
  assert.match(styles,/@media\(min-width:900px\)[\s\S]*\.page\[data-page="home"\]\.active[\s\S]*grid-template-columns/);
  // Cards flow into whichever column is shortest so far, so a short card
  // never leaves a gap below it next to a tall neighbour (a plain 2-column
  // grid would lock every row's height to its tallest cell).
  assert.match(styles,/\.page\[data-page="settings"\]\.active\{[\s\S]*column-count:2/);
  assert.match(styles,/\.page\[data-page="settings"\]>\.card\{[\s\S]*break-inside:avoid/);
  assert.match(styles,/max-width:1360px/);
  assert.match(styles,/body\.wide \.page\[data-page="buffer"\]\{[\s\S]*max-width:none/);
});


test('redesign uses today target without subtracting expenses twice and preserves overspending', async () => {
 const {get,responses}=setup();
 responses['/api/dashboard'].spent_today=2190;
 Object.assign(responses['/api/cashflow'],{today_target:1272.13,available_today:-917.87});
 await get('refreshBtn').listeners.click();
 assert.match(get('dailyAvailable').textContent,/-917,87/);
 assert.match(get('leftToday').textContent,/-917,87/);
 assert.equal(get('spendFill').style.width,'100%');
 Object.assign(responses['/api/cashflow'],{today_target:4255,available_today:2975});
 responses['/api/dashboard'].spent_today=1280;
 await get('refreshBtn').listeners.click();
 assert.match(get('dailyAvailable').textContent,/4.255,00/);
 assert.match(get('leftToday').textContent,/2.975,00/);
});

test('buffer renders responsive cards and table, escaping user-provided labels', async () => {
 const {get,responses}=setup();
 responses['/api/cashflow'].periods=[
 {start:'2026-09-07',end:'2026-09-21',kind:'<img src=x>',days:15,received:30000,mandatory:5000,free:25000,to_card:15000,daily:1000,put_aside:10000,take:0,buffer:10000},
 {start:'2026-09-22',end:'2026-10-06',kind:'Аванс',days:15,received:20000,mandatory:10000,free:10000,to_card:15000,daily:1000,put_aside:0,take:5000,buffer:5000}
 ];
 await get('refreshBtn').listeners.click();
 assert.match(get('planCards').innerHTML,/&lt;img/);
 assert.doesNotMatch(get('bufferPeriods').innerHTML,/<img/);
 assert.match(get('bufWarn').textContent,/5.000/);
 assert.match(get('planDaily').textContent,/В день везде/);
 assert.match(get('bufferHead').innerHTML,/Движение буфера/);
 assert.match(get('bufferPeriods').innerHTML,/5.000,00/);
 assert.match(get('bufferPeriods').innerHTML,/из буфера/);
 assert.doesNotMatch(get('bufferPeriods').innerHTML,/−5.000,00/);
 // The "На карту" cell shows the card money on top and the daily rate below.
 assert.match(get('bufferPeriods').innerHTML,/15.000,00.₽<\/strong><br><small>1.000,00.₽ в день/);
 assert.match(get('planCards').innerHTML,/15.000,00.₽<br><small>1.000,00.₽ в день/);
});

test('start row shows the start capital as its money and the horizon with a year', async () => {
 const {get,responses}=setup();
 Object.assign(responses['/api/cashflow'],{horizon_end:'2027-09-23'});
 responses['/api/cashflow'].periods=[
 {start:'2026-09-23',end:'2026-10-06',kind:'Старт',payday:null,days:14,received:0,mandatory:27700,free:-27700,to_card:20269.95,daily:1351.33,buffer_start:68534.33,put_aside:0,take:47969.95,buffer:20564.38},
 {start:'2026-10-07',end:'2026-10-21',kind:'Зарплата',payday:'2026-10-07',days:15,received:81199.72,mandatory:41900,free:39299.72,to_card:20270.10,daily:1351.33,buffer_start:20564.38,put_aside:0,take:970.38,buffer:19594}
 ];
 await get('refreshBtn').listeners.click();
 const table=get('bufferPeriods').innerHTML;
 assert.match(table,/68.534,33/);
 assert.match(table,/40.834,33/);
 assert.match(table,/20.564,38.₽<\/span><small>в буфер/);
 assert.doesNotMatch(table,/47.969,95/);
 assert.doesNotMatch(get('bufWarn').textContent,/23 сент/);
 assert.match(get('bufferHorizon').textContent,/2027/);
});

test('future received amount can be edited and sent for budget recalculation', async () => {
 const {sandbox,get,requests,responses}=setup();
 responses['/api/cashflow'].periods=[
  {start:'2026-09-15',end:'2026-09-21',kind:'сейчас',days:7,payday:null,received:26246.45,planned_received:26246.45,received_editable:false,income_overridden:false,override_key:null,mandatory:0,free:26246.45,daily:1000,put_aside:0,take:0,buffer:10000,to_card:0},
  {start:'2026-09-22',end:'2026-10-06',kind:'Аванс',days:15,payday:'2026-09-22',received:39395.22,planned_received:39395.22,received_editable:true,income_overridden:false,override_key:'2026-09-23',payday_amount:39395.22,planned_payday_amount:39395.22,mandatory:10000,free:29395.22,daily:1000,put_aside:10000,take:0,buffer:20000,to_card:10000}
 ];
 await get('refreshBtn').listeners.click();
 assert.match(get('planCards').innerHTML,/editCashflowIncome\('2026-09-23'\)/);

 sandbox.window.editCashflowIncome('2026-09-23');
 assert.equal(get('cashflowIncomeAmount').value,39395.22);
 get('cashflowIncomeAmount').value='41000';
 await get('cashflowIncomeForm').listeners.submit({preventDefault(){}});

 const request=requests.find(row=>row.url==='/api/cashflow/income-overrides/2026-09-23');
 assert.deepEqual(JSON.parse(request.body),{amount:41000});
});

test('overspend panel appears with a positive overspend and the spread button hides it', async () => {
 const {get,responses}=setup();
 Object.assign(responses['/api/cashflow'],{overspend:1000,available_today:-1000,today_target:100});
 await get('refreshBtn').listeners.click();
 assert.equal(get('overspendPanel').classList.contains('hidden'),false);
 assert.match(get('overspendAmount').textContent,/1.000,00/);
 get('spreadOverspendBtn').listeners.click();
 assert.equal(get('overspendPanel').classList.contains('hidden'),true);
});

test('overspend panel is hidden without an overspend', async () => {
 const {get,responses}=setup();
 Object.assign(responses['/api/cashflow'],{overspend:0});
 await get('refreshBtn').listeners.click();
 assert.equal(get('overspendPanel').classList.contains('hidden'),true);
});

test('cover-overspend button opens the piggy dialog set up for the transfer', async () => {
 const {get}=setup();
 await get('refreshBtn').listeners.click();
 get('coverFromPiggyBtn').listeners.click();
 assert.equal(get('piggyForm').dataset.mode,'coverOverspend');
 assert.equal(get('piggyPurpose').value,'cover_overspend');
});

test('piggy to-card transfer posts to the dedicated endpoint with its purpose', async () => {
 const {get,requests}=setup();
 await get('refreshBtn').listeners.click();
 get('piggyToCardBtn').listeners.click();
 assert.equal(get('piggyForm').dataset.mode,'toCard');
 get('piggyAmount').value='500';
 await get('piggyForm').listeners.submit({preventDefault(){}});
 const request=requests.find(row=>row.url==='/api/piggy-bank/to-card');
 assert.ok(request,'expected a request to /api/piggy-bank/to-card');
 const body=JSON.parse(request.body);
 assert.equal(body.amount,500);
 assert.equal(body.purpose,'transfer');
});

test('localized money is parsed before expense submission', async () => {
  const {get,requests} = setup();
  get('expenseAmount').value = '2 070,00';
  get('expenseDate').value = '2026-09-15';
  get('expenseCategory').value = '1';
  await get('expenseForm').listeners.submit({preventDefault(){},target:get('expenseForm')});
  const request = requests.find(item => item.url === '/api/transactions' && item.body);
  assert.equal(JSON.parse(request.body).amount,2070);
});
