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
  for (const name of ['budget-date.js','money-input.js','app.js','cashflow-ui.js','buffer-ui.js','design-ui.js','irregular-ui.js']) {
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
 // The limit stays the limit; the overspend is the (negative) remaining amount.
 assert.match(get('dailyAvailable').textContent,/1.272,13/);
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
 assert.equal(body.purpose,'today');
 assert.equal(get('piggyPurposeField').classList.contains('hidden'),false);
});

test('piggy to-card transfer can be spread over the period', async () => {
 const {get,requests}=setup();
 await get('refreshBtn').listeners.click();
 get('piggyToCardBtn').listeners.click();
 get('piggyPurposeChoice').value='transfer';
 get('piggyAmount').value='500';
 await get('piggyForm').listeners.submit({preventDefault(){}});
 const request=requests.find(row=>row.url==='/api/piggy-bank/to-card');
 assert.equal(JSON.parse(request.body).purpose,'transfer');
});

test('operations list shows card and piggy bank transfers', async () => {
 const {get,responses}=setup();
 responses['/api/piggy-bank']={balance:100,movements:[
  {id:5,direction:'withdraw',amount:1400,movement_date:'2026-09-15',note:'интернет',source:'daily_budget',purpose:'today'},
  {id:6,direction:'deposit',amount:50,movement_date:'2026-09-15',note:'',source:'daily_budget',purpose:null,bill_payment_id:9},
  {id:7,direction:'deposit',amount:70,movement_date:'2026-09-15',note:'',source:'external',purpose:null},
 ]};
 await get('refreshBtn').listeners.click();
 const html=get('transactionsList').innerHTML;
 assert.match(html,/Копилка → карта/);
 assert.match(html,/на сегодня/);
 assert.match(html,/deletePiggyMovement\(5\)/);
 assert.doesNotMatch(html,/deletePiggyMovement\(6\)/);
 assert.doesNotMatch(html,/deletePiggyMovement\(7\)/);
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

test('home screen lists upcoming unpaid bills and the latest operations', async () => {
  const {get,responses} = setup();
  responses['/api/plan'] = [
    {id:1,title:'Аренда',amount:35000,due_date:'2099-10-05',paid:false},
    {id:2,title:'<b>Связь</b>',amount:450,due_date:'2099-09-30',paid:false},
    {id:3,title:'Кредит',amount:12000,due_date:'2026-09-15',paid:true},
  ];
  responses['/api/transactions'] = [
    {id:9,type:'expense',amount:897,tx_date:'2026-09-15',note:'Пятёрочка',category_title:'Продукты',category_emoji:'🛒'},
    {id:8,type:'income',amount:5000,tx_date:'2026-09-14',note:'Премия'},
  ];
  await get('refreshBtn').listeners.click();
  const bills = get('homeBills').innerHTML;
  assert.match(bills, /&lt;b&gt;Связь[\s\S]*Аренда/);
  assert.doesNotMatch(bills, /Кредит/);
  const recent = get('homeRecent').innerHTML;
  assert.match(recent, /Пятёрочка[\s\S]*Продукты · 15 сент\.[\s\S]*−897,00/);
  assert.match(recent, /class="amount income">\+5[\s ]000,00/);
});

test('operations can be searched and filtered by type', async () => {
  const {sandbox,get,responses} = setup();
  responses['/api/transactions'] = [
    {id:1,type:'expense',amount:100,tx_date:'2026-09-15',note:'Кофе',category_title:'Кафе'},
    {id:2,type:'income',amount:500,tx_date:'2026-09-14',note:'Возврат'},
    {id:3,type:'expense',amount:900,tx_date:'2026-09-10',bill_rule_id:4,bill_title:'Интернет'},
  ];
  await get('refreshBtn').listeners.click();
  get('txSearch').value = 'кафе';
  vm.runInContext('renderTransactions()', sandbox);
  assert.match(get('transactionsList').innerHTML, /Кофе/);
  assert.doesNotMatch(get('transactionsList').innerHTML, /Возврат|Интернет/);
  get('txSearch').value = '';
  vm.runInContext('transactionFilter="bills"; renderTransactions()', sandbox);
  assert.match(get('transactionsList').innerHTML, /Интернет/);
  assert.doesNotMatch(get('transactionsList').innerHTML, /Кофе|Возврат/);
  get('txSearch').value = 'нет такого';
  vm.runInContext('renderTransactions()', sandbox);
  assert.match(get('transactionsList').innerHTML, /Ничего не найдено/);
});


const IRREGULAR = {
  enabled:true, configured:true, today:'2026-10-03', start_date:'2026-10-01',
  settings:{budget_mode:'irregular',reserve_percent:10,reserve_target:null,stretch_days:14,lookahead_days:30,start_date:'2026-10-01',start_total:0,start_reserve:0},
  today_target:785.71, available_today:785.71, spent_today:0, overspend:0, tomorrow_limit:846.15,
  free_balance:11000, stretch_until:'2026-10-16', days_left:14, window_extended:false,
  reserve_balance:2000, avg_daily_spending:500, reserve_days:4, bills_reserved:7000,
  next_bill:{id:1,title:'Интернет',due_date:'2026-10-10',amount:1000,reserved:1000,missing:0,paid:false},
  bills_shortfall:{amount:2500,date:'2026-10-25'}, last_income_date:'2026-10-03', days_without_income:0,
  no_income_warning:false, piggy_bank_balance:0,
  bills:[{id:1,title:'Интернет',due_date:'2026-10-10',amount:1000,reserved:1000,missing:0,paid:false,payment_id:null,paid_amount:null}],
};

function irregularSetup() {
  const env = setup();
  env.responses['/api/bootstrap'].settings.budget_mode = 'irregular';
  env.responses['/api/bootstrap'].categories = [{id:5,title:'Здоровье',emoji:'💊'}];
  env.responses['/api/irregular'] = JSON.parse(JSON.stringify(IRREGULAR));
  env.responses['/api/irregular/reserve'] = {balance:2000,movements:[
    {id:null,income_id:3,kind:'income',title:'Процент от дохода',date:'2026-10-03',amount:2000,balance:2000,reason:'ремонт',expense_id:null}]};
  env.responses['/api/irregular/preview'] = {reserve:2000,bills:7000,free:11000,piggy:0,amount:20000,percent:10,stretch_until:'2026-10-16',
    summary:'Пришло 20 000 ₽ → 2 000 ₽ в резерв (10%), 7 000 ₽ на обязательные, 11 000 ₽ свободных'};
  env.responses['/api/irregular/incomes'] = {id:9,summary:'Пришло 20 000 ₽ → 2 000 ₽ в резерв (10%), 7 000 ₽ на обязательные, 11 000 ₽ свободных'};
  return env;
}

test('irregular mode shows its own home numbers and warnings', async () => {
  const {get} = irregularSetup();
  await get('refreshBtn').listeners.click();
  assert.match(get('dailyAvailable').textContent,/785,71/);
  assert.match(get('irrFree').textContent,/11[\s ]000,00/);
  assert.equal(get('irrStretch').textContent,'Растягиваем до 16.10 (осталось 14 дн.)');
  assert.match(get('irrReserve').textContent,/2[\s ]000,00/);
  assert.match(get('irrReserveDays').textContent,/4 дня/);
  assert.match(get('irrNextBill').textContent,/Интернет/);
  assert.match(get('irrBillsWarning').textContent,/^На обязательные не хватает 2[\s ]500,00[\s ]₽ к 25\.10$/);
  assert.equal(get('irrBillsWarning').classList.contains('hidden'),false);
  assert.equal(get('irrNoIncomeWarning').classList.contains('hidden'),true);
  assert.match(get('irrReserveMovements').innerHTML,/Процент от дохода/);
  assert.match(get('irrObligationsList').innerHTML,/отложено полностью/);
});

test('no income for a long time offers to take money from the reserve', async () => {
  const {get,responses} = irregularSetup();
  Object.assign(responses['/api/irregular'],{no_income_warning:true,days_without_income:17,window_extended:true});
  await get('refreshBtn').listeners.click();
  assert.equal(get('irrNoIncomeWarning').classList.contains('hidden'),false);
  assert.match(get('irrNoIncomeText').textContent,/Дохода не было 17 дней/);
  get('irrTakeReserveBtn').listeners.click();
  assert.equal(get('irrReserveMode').value,'withdraw');
  assert.equal(get('irrReservePurpose').value,'to_free');
});

test('income dialog fills the percent, previews the split and saves', async () => {
  const {sandbox,get,requests} = irregularSetup();
  await get('refreshBtn').listeners.click();
  get('quickIncomeBtn').listeners.click();
  assert.equal(get('irrIncomePercent').value,10);
  assert.equal(get('irrIncomeDestination').value,'split');
  get('irrIncomeAmount').value = '20 000';
  get('irrIncomeSource').value = 'ремонт';
  await sandbox.window.irregularUi.preview();
  const previewRequest = requests.find(r => r.url === '/api/irregular/preview');
  assert.deepEqual(JSON.parse(previewRequest.body),{amount:20000,tx_date:get('irrIncomeDate').value,reserve_percent:10,destination:'split',exclude_id:null});
  assert.match(get('irrIncomePreview').innerHTML,/На обязательные/);
  assert.match(get('irrIncomePreview').innerHTML,/11 000 ₽ свободных/);
  await get('irrIncomeForm').listeners.submit({preventDefault(){}});
  const saved = requests.find(r => r.url === '/api/irregular/incomes');
  const body = JSON.parse(saved.body);
  assert.equal(body.amount,20000);
  assert.equal(body.income_source,'ремонт');
  assert.equal(body.reserve_percent,10);
  assert.equal(body.destination,'split');
  assert.match(get('toast').textContent,/Пришло 20 000 ₽ → 2 000 ₽ в резерв \(10%\)/);
});

test('editing an income in irregular mode opens the income dialog with its own percent', async () => {
  const {sandbox,get,responses} = irregularSetup();
  responses['/api/transactions'] = [{id:4,type:'income',amount:5000,tx_date:'2026-10-02',income_source:'доставка',reserve_percent:5,income_destination:'reserve',note:''}];
  await get('refreshBtn').listeners.click();
  sandbox.window.editTransaction(4);
  assert.equal(get('irrIncomeId').value,4);
  assert.equal(get('irrIncomePercent').value,5);
  assert.equal(get('irrIncomeDestination').value,'reserve');
  assert.match(get('transactionsList').innerHTML,/доставка/);
});

test('paying an expense from the reserve sends the category and the reason', async () => {
  const {sandbox,get,requests} = irregularSetup();
  await get('refreshBtn').listeners.click();
  sandbox.window.irregularUi.openReserve('withdraw','pay_expense');
  get('irrReserveAmount').value = '1 500';
  get('irrReserveCategory').value = '5';
  get('irrReserveReason').value = '';
  await get('irrReserveForm').listeners.submit({preventDefault(){}});
  assert.equal(requests.filter(r => r.url === '/api/irregular/reserve/withdraw').length,0);
  assert.match(get('toast').textContent,/Укажите причину/);
  get('irrReserveReason').value = 'Стоматолог';
  await get('irrReserveForm').listeners.submit({preventDefault(){}});
  const body = JSON.parse(requests.find(r => r.url === '/api/irregular/reserve/withdraw').body);
  assert.deepEqual({...body,movement_date:undefined},{amount:1500,movement_date:undefined,purpose:'pay_expense',reason:'Стоматолог',category_id:5});
});

test('payroll mode keeps the old income dialog and never asks for the reserve', async () => {
  const {get,requests} = setup();
  await get('refreshBtn').listeners.click();
  get('quickIncomeBtn').listeners.click();
  assert.equal(get('irrIncomeAmount').value,'');
  assert.match(get('incomeTxDate').value,/^\d{4}-\d{2}-\d{2}$/);
  assert.equal(requests.filter(r => r.url === '/api/irregular/reserve').length,0);
});

test('switching to side jobs without a start asks for it first', async () => {
  const {sandbox,get,requests,responses} = setup();
  responses['/api/irregular'] = {enabled:false,configured:false,settings:{budget_mode:'payroll',reserve_percent:10,stretch_days:14,lookahead_days:30}};
  await get('refreshBtn').listeners.click();
  await sandbox.window.irregularUi.chooseMode('irregular');
  assert.equal(requests.filter(r => r.url === '/api/budget-mode').length,0);
  assert.equal(get('irrStretchDays').value,14);
  get('irrStartTotal').value = '30 000';
  get('irrStartReserve').value = '5 000';
  await sandbox.window.irregularUi.saveSettings();
  const mode = JSON.parse(requests.find(r => r.url === '/api/budget-mode').body);
  assert.equal(mode.budget_mode,'irregular');
  assert.equal(mode.start_total,30000);
  assert.equal(mode.start_reserve,5000);
});
