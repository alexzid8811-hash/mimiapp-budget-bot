const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

let state = { bootstrap: null, payroll: null, vacations: [], dashboard: null, transactions: [], plan: [] };
const $ = (id) => document.getElementById(id);

function getTelegramInitData() {
  const sdkInitData = window.Telegram?.WebApp?.initData;
  if (sdkInitData) return sdkInitData;

  // Telegram Desktop can expose Web App data in the URL before the SDK
  // finishes copying it to Telegram.WebApp.initData.
  for (const source of [window.location.hash.slice(1), window.location.search.slice(1)]) {
    const urlInitData = new URLSearchParams(source).get("tgWebAppData");
    if (urlInitData) return urlInitData;
  }
  return "";
}

function requestHeaders(extraHeaders = {}) {
  const requestHeaders = { "Content-Type": "application/json", ...extraHeaders };
  const initData = getTelegramInitData();
  if (initData) requestHeaders["X-Telegram-Init-Data"] = initData;
  return requestHeaders;
}

function toast(message) {
  const el = $("toast"); el.textContent = message; el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 1800);
}

function apiErrorMessage(body, status) {
  const detail = body?.detail;
  if (Array.isArray(detail)) {
    return detail.map(item => item.msg || item.message || JSON.stringify(item)).join("; ");
  }
  if (detail && typeof detail === "object") {
    return detail.msg || detail.message || JSON.stringify(detail);
  }
  return detail || `Ошибка ${status}`;
}

async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: requestHeaders(options.headers) });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(apiErrorMessage(body, res.status));
  }
  return res.status === 204 ? null : res.json();
}

function currency() { return "RUB"; }
function money(v) {
  return new Intl.NumberFormat("ru-RU", { style: "currency", currency: currency(), minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(v || 0));
}
function moneyValue(id) {
  const value = $(id)?.value || "0";
  return window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(value) : Number(value) || 0;
}
function fmtDate(s) { return new Intl.DateTimeFormat("ru-RU", { day:"numeric", month:"short" }).format(new Date(`${s}T12:00:00`)); }
const MONTHS_GENITIVE = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
function fmtMonth(year, month) { return new Intl.DateTimeFormat("ru-RU", { month:"long", year:"numeric" }).format(new Date(year, month - 1, 1)); }
function todayISO() { return window.budgetDate.today(); }
function shiftISODate(iso, days) { return window.budgetDate.shift(iso, days); }

let loadVersion = 0;

// The operations list holds the latest operations and every operation since
// transactionsFrom(): «Период» shows a whole card period.  The first load
// does not know the period yet.
let transactionsSince = null;

async function loadAll() {
  const version = ++loadVersion;
  const since = transactionsFrom();
  try {
    const [bootstrap, payroll, vacations, dashboard, transactions, plan, cashflowSettings, cashflow, piggy, irregular, accounts] = await Promise.all([
      api("/api/bootstrap"), api("/api/payroll-settings"), api("/api/vacations"), api("/api/dashboard"), api(since ? `/api/transactions?since=${since}` : "/api/transactions"), api("/api/plan"), api("/api/cashflow-settings"), api("/api/cashflow"),
      api(since ? `/api/piggy-bank?since=${since}` : "/api/piggy-bank").catch(() => ({ movements: [] })),
      api("/api/irregular").catch(() => null),
      api("/api/accounts").catch(() => null)
    ]);
    if (version !== loadVersion) return;
    window.budgetDate.configure(bootstrap.budget_timezone);
    state = { bootstrap, payroll, vacations, dashboard, transactions, plan, cashflowSettings, cashflow, piggy, irregular, accounts };
    transactionsSince = since;
    window.accountsUi?.render(state);
    render();
    // A period picked with ‹ while this load was running is loaded now.
    if (transactionRange === "period") coverTransactionPeriod();
    window.applyCashflowSettings?.(cashflowSettings);
    window.applyCashflow?.(cashflow);
    await window.refreshBuffer?.(cashflow);
    window.budgetDesign?.render(state);
    await window.irregularUi?.render(state);
  } catch (e) { toast(e.message); }
}

function isIrregular() {
  return state.bootstrap?.settings?.budget_mode === "irregular";
}

function render() {
  renderHeader(); renderDashboard(); renderHomeLists(); renderTransactions(); renderPlan(); renderSettings(); fillCategorySelects();
}

function daysUntil(iso) {
  return Math.round((Date.parse(`${iso}T12:00:00Z`) - Date.parse(`${todayISO()}T12:00:00Z`)) / 86400000);
}

function dueLabel(iso) {
  const n = daysUntil(iso);
  if (n < 0) return "просрочен";
  if (n === 0) return "сегодня";
  if (n === 1) return "завтра";
  const word = n % 100 >= 11 && n % 100 <= 14 ? "дней" : n % 10 === 1 ? "день" : n % 10 >= 2 && n % 10 <= 4 ? "дня" : "дней";
  return `через ${n} ${word}`;
}

// Short lists on the home screen: the next unpaid bills and the latest operations.
function renderHomeLists() {
  const bills = $("homeBills");
  if (bills) {
    const upcoming = (state.plan || []).filter(p => !p.paid)
      .sort((a, b) => a.due_date.localeCompare(b.due_date)).slice(0, 3);
    bills.innerHTML = upcoming.length ? upcoming.map(p => `<div class="list-row compact-row"><div class="row-main"><div class="emoji">${escapeHtml(p.category_emoji) || "📌"}</div><div class="row-text"><div class="row-title">${escapeHtml(p.title)}</div><div class="row-sub">${fmtDate(p.due_date)} · ${dueLabel(p.due_date)}</div></div></div><div class="amount">${money(p.amount)}</div></div>`).join("")
      : `<div class="empty">В этом периоде всё оплачено.</div>`;
  }
  const recent = $("homeRecent");
  if (recent) {
    const items = (state.transactions || []).slice(0, 4);
    recent.innerHTML = items.length ? items.map(t => {
      const isExpense = t.type === "expense";
      const title = t.bill_title || t.note || t.category_title || (isExpense ? "Расход" : "Доход");
      const sub = [t.category_title, fmtDate(t.tx_date)].filter(Boolean).map(escapeHtml).join(" · ");
      return `<div class="list-row compact-row"><div class="row-main"><div class="emoji">${escapeHtml(t.category_emoji) || (isExpense ? "💳" : "💰")}</div><div class="row-text"><div class="row-title">${escapeHtml(title)}</div><div class="row-sub">${sub}</div></div></div><div class="amount ${isExpense ? "" : "income"}">${isExpense ? "−" : "+"}${money(t.amount)}</div></div>`;
    }).join("") : `<div class="empty">Пока нет операций.</div>`;
  }
}

function renderHeader() {
  const name = state.bootstrap?.user?.first_name;
  $("hello").textContent = name ? `Бюджет, ${name}` : "Мой бюджет";
}

function renderDashboard() {
  const d = state.dashboard; if (!d) return;
  $("dailyAvailable").textContent = money(d.daily_available);
  $("periodCaption").textContent = `${fmtDate(d.period.start)} — ${fmtDate(d.period.end)} · ${d.period.days_left} дн.`;
  const cashNote = $("cardCashNote");
  if (cashNote) cashNote.textContent = `на ${d.period.days_left} дн. периода`;
  $("spentToday").textContent = money(d.spent_today);
  // Fallback values until the detailed cash-flow snapshot loads.
  $("cardCash").textContent = money(d.remaining);
  $("cardBuffer").textContent = money(d.reserve.balance);
  $("cardPiggy").textContent = "—";
  $("cardReservesTotal").textContent = money(d.reserve.balance);
  $("reserveReason").textContent = d.reserve.reason;
  const pct = d.period_budget > 0 ? Math.min(100, Math.max(0, d.spent / d.period_budget * 100)) : 0;
  $("dayProgress").style.width = `${pct}%`;
  $("forecastList").innerHTML = d.forecast.length ? d.forecast.map(p => `
    <div class="list-row"><div class="row-text"><div class="row-title">${fmtDate(p.start)} — ${fmtDate(p.end)}</div><div class="row-sub"><span class="nowrap">Доход ${money(p.income)}</span> · <span class="nowrap">платежи ${money(p.mandatory)}</span></div></div><div class="amount ${p.net < 0 ? 'expense':'income'}">${p.net >= 0 ? '+' : ''}${money(p.net)}</div></div>`).join("") : `<div class="empty">Добавьте суммы зарплаты и аванса, чтобы увидеть прогноз.</div>`;
}

const COLLAPSED_TRANSACTION_WEEKS_KEY = "budget-collapsed-transaction-weeks";
let collapsedTransactionWeeks = (() => {
  try {
    const saved = JSON.parse(window.localStorage?.getItem(COLLAPSED_TRANSACTION_WEEKS_KEY) || "[]");
    return new Set(Array.isArray(saved) ? saved : []);
  } catch (_) {
    return new Set();
  }
})();

function transactionWeekStart(iso) {
  const day = new Date(`${iso}T12:00:00`).getDay();
  return shiftISODate(iso, -((day + 6) % 7));
}

function transactionWeekTitle(start) {
  const end = shiftISODate(start, 6);
  return `${fmtDate(start)} — ${fmtDate(end)}`;
}

function toggleTransactionWeek(weekStart) {
  if (collapsedTransactionWeeks.has(weekStart)) collapsedTransactionWeeks.delete(weekStart);
  else collapsedTransactionWeeks.add(weekStart);
  try {
    window.localStorage?.setItem(COLLAPSED_TRANSACTION_WEEKS_KEY, JSON.stringify([...collapsedTransactionWeeks]));
  } catch (_) {}
  renderTransactions();
}

function renderTransactionRow(t) {
  const isExpense = t.type === "expense";
  const title = t.bill_title || t.note || t.category_title || (isExpense ? "Расход" : "Доход");
  const editPayment = t.bill_rule_id
    ? `<button class="tiny" onclick="editBillPayment(${t.id})">Изменить</button>`
    : `<button class="tiny" onclick="editTransaction(${t.id})">Изменить</button>`;
  const source = !isExpense && t.income_source ? ` · ${escapeHtml(t.income_source)}` : '';
  const account = window.accountsUi?.accountLabel(t);
  const accountText = account ? ` · ${account}` : '';
  return `<div class="list-row"><div class="row-main"><div class="emoji">${escapeHtml(t.category_emoji) || (isExpense ? '💳':'💰')}</div><div class="row-text"><div class="row-title">${escapeHtml(title)}</div><div class="row-sub">${fmtDate(t.tx_date)}${t.category_title ? ` · ${escapeHtml(t.category_title)}`:''}${source}${accountText}</div></div></div><div><div class="amount ${t.type}">${isExpense?'-':'+'}${money(t.amount)}</div><div class="actions">${editPayment}<button class="tiny danger" onclick="deleteTx(${t.id})">Удалить</button></div></div></div>`;
}

// Transfers between the card and the piggy bank change the day's money, so
// they are listed with the operations.  Movements created by an income or a
// bill payment are already shown through that operation.
function cardPiggyTransfers() {
  return (state.piggy?.movements || [])
    .filter(m => m.source === "daily_budget" && !m.bill_payment_id && !m.income_transaction_id)
    .map(m => ({ ...m, type: "transfer", tx_date: m.movement_date, piggy: true }));
}

function renderPiggyTransferRow(m) {
  const toCard = m.direction === "withdraw";
  const title = toCard ? "Копилка → карта" : "Карта → копилка";
  const how = !toCard ? "" : m.purpose === "transfer" ? " · по дням периода" : " · на сегодня";
  const note = m.note ? ` · ${escapeHtml(m.note)}` : "";
  return `<div class="list-row"><div class="row-main"><div class="emoji">🐷</div><div class="row-text"><div class="row-title">${title}</div><div class="row-sub">${fmtDate(m.movement_date)}${how}${note}</div></div></div><div><div class="amount ${toCard ? "income" : "expense"}">${toCard ? "+" : "-"}${money(m.amount)}</div><div class="actions"><button class="tiny danger" onclick="deletePiggyMovement(${m.id})">Удалить</button></div></div></div>`;
}

// Mandatory bill payments have their own tab, so the everyday list (and its
// weekly totals) only holds day-to-day spending, income and piggy transfers.
let transactionView = "daily";
let transactionFilter = "all";
// «Сегодня» and «Период» (the card period in the header) combine with the
// type chips: «Сегодня» + «Расходы» is today's spending.
let transactionRange = "all";
// The period «Период» shows after ‹: null is the current one.
let transactionPeriod = null;

// The earliest day the operations list must hold: the start of the period on
// screen, the current one at least.
function transactionsFrom() {
  return [state.dashboard?.period?.start, transactionPeriod?.start].filter(Boolean).sort()[0] || null;
}

// Loads operations and card ↔ piggy transfers back to transactionsFrom() when
// the list does not reach that far yet.
async function coverTransactionPeriod() {
  const since = transactionsFrom();
  if (!since || (transactionsSince && transactionsSince <= since)) return;
  const version = loadVersion;
  try {
    const [transactions, piggy] = await Promise.all([
      api(`/api/transactions?since=${since}`), api(`/api/piggy-bank?since=${since}`),
    ]);
    // A newer load replaces the list and covers the period itself.
    if (version !== loadVersion) return;
    state.transactions = transactions;
    state.piggy = piggy;
    transactionsSince = since;
    renderTransactions();
  } catch (e) { toast(e.message); }
}
const TRANSACTION_FILTERS = {
  all: () => true,
  expense: t => !t.piggy && t.type === "expense",
  income: t => !t.piggy && t.type === "income",
  piggy: t => Boolean(t.piggy),
};

function inTransactionView(t) {
  return transactionView === "bills" ? Boolean(t.bill_rule_id) : !t.bill_rule_id;
}

function transactionMatches(t, query) {
  if (transactionView !== "bills" && !(TRANSACTION_FILTERS[transactionFilter] || TRANSACTION_FILTERS.all)(t)) return false;
  if (!query) return true;
  const text = [t.note, t.category_title, t.bill_title, t.piggy ? "копилка" : ""].join(" ").toLowerCase();
  return text.includes(query);
}

function renderTransactions() {
  const el = $("transactionsList");
  const query = String($("txSearch")?.value || "").trim().toLowerCase();
  const all = [...state.transactions, ...cardPiggyTransfers()].filter(inTransactionView);
  const daily = transactionView !== "bills";
  const today = daily && transactionRange === "today" ? todayISO() : null;
  const period = daily && transactionRange === "period" && !isIrregular() ? transactionPeriod || state.dashboard?.period : null;
  $("txPeriodNav")?.classList.toggle("hidden", !period);
  if (period) {
    $("txPeriodLabel").textContent = `${fmtDate(period.start)} — ${fmtDate(period.end)}`;
    $("txPeriodNext").disabled = !transactionPeriod;
  }
  const inRange = t => today ? t.tx_date === today : !period || (t.tx_date >= period.start && t.tx_date <= period.end);
  const items = all.filter(t => inRange(t) && transactionMatches(t, query))
    .sort((a, b) => b.tx_date.localeCompare(a.tx_date) || String(b.created_at || "").localeCompare(String(a.created_at || "")));
  if (!items.length) {
    const message = !all.length
      ? (transactionView === "bills" ? "Оплаченных обязательных платежей пока нет." : "Пока нет операций.")
      : query || transactionFilter !== "all" ? "Ничего не найдено."
        : today ? "Сегодня операций пока нет." : period ? "В этом периоде операций пока нет." : "Ничего не найдено.";
    el.innerHTML = `<div class="empty">${message}</div>`;
    return;
  }

  // With «Сегодня» or «Период» on, the one group is today or the period instead of weeks.
  const weeks = new Map();
  items.forEach(t => {
    const weekStart = today ? "today" : period ? "period" : transactionWeekStart(t.tx_date);
    if (!weeks.has(weekStart)) weeks.set(weekStart, []);
    weeks.get(weekStart).push(t);
  });

  el.innerHTML = [...weeks.entries()]
    .sort(([a], [b]) => b.localeCompare(a))
    .map(([weekStart, transactions]) => {
      const collapsed = collapsedTransactionWeeks.has(weekStart);
      const expenseTotal = transactions
        .filter(t => t.type === "expense")
        .reduce((sum, t) => sum + Number(t.amount || 0), 0);
      const n = transactions.length, tens = n % 100, ones = n % 10;
      const countLabel = `${n} ${tens >= 11 && tens <= 14 ? "операций" : ones === 1 ? "операция" : ones >= 2 && ones <= 4 ? "операции" : "операций"}`;
      return `<section class="transaction-week ${collapsed ? "collapsed" : ""}">
        <button class="transaction-week-toggle" type="button" onclick="toggleTransactionWeek('${weekStart}')" aria-expanded="${!collapsed}">
          <span><strong>${today ? `Сегодня, ${fmtDate(today)}` : period ? `${fmtDate(period.start)} — ${fmtDate(period.end)}` : transactionWeekTitle(weekStart)}</strong><small>${countLabel}</small></span>
          <span class="transaction-week-summary">${expenseTotal ? `−${money(expenseTotal)}` : ""}<i aria-hidden="true">⌄</i></span>
        </button>
        <div class="transaction-week-rows">${transactions.map(t => t.piggy ? renderPiggyTransferRow(t) : renderTransactionRow(t)).join("")}</div>
      </section>`;
    }).join("");
}

function renderPlan() {
  const el = $("planList");
  // In the irregular mode payments of later months appear once their month comes.
  const month = todayISO().slice(0, 7);
  const plan = isIrregular() ? state.plan.filter(p => p.due_date.slice(0, 7) <= month) : state.plan;
  if (!plan.length) { el.innerHTML = `<div class="empty">${isIrregular() ? "В этом месяце" : "В этом периоде"} обязательных платежей нет.</div>`; return; }
  el.innerHTML = plan.map(p => {
    const planned = Number(p.planned_amount ?? p.amount);
    const paidCaption = Number(p.amount) === planned
      ? 'оплачено'
      : `оплачено из ${money(planned)}`;
    const remainderCaption = Number(p.remainder_amount || 0) > 0
      ? ` · ${money(p.remainder_amount)} в копилке`
      : '';
    // The button names the action; whether the bill is paid is said next to its date.
    const action = p.paid
      ? `<div class="bill-payment-action"><button class="tiny" onclick="editBillPayment(${p.payment_id})">Изменить</button></div>`
      : `<div class="bill-payment-action"><button class="tiny" onclick="payBill(${p.id},'${p.due_date}')">Отметить оплату</button></div>`;
    // In the irregular mode an unpaid bill shows the money put aside for it.
    const meta = p.paid ? `${paidCaption}${remainderCaption}`
      : p.reserved === undefined ? "уже учтено в бюджете"
      : Number(p.missing) > 0 ? `отложено ${money(p.reserved)}, не хватает ${money(p.missing)}` : `отложено ${money(p.reserved)}`;
    return `<div class="list-row bill-row ${p.paid?'paid':''}"><div class="row-main"><div class="emoji">${escapeHtml(p.category_emoji) || '📌'}</div><div class="row-text"><div class="bill-heading"><div class="row-title">${escapeHtml(p.title)}</div><div class="amount expense">${money(p.amount)}</div></div><div class="row-sub bill-meta">${fmtDate(p.due_date)} · ${meta}</div></div></div>${action}</div>`;
  }).join("");
}

function renderSettings() {
  const b = state.bootstrap; if (!b) return;
  const settings = b.settings || {};
  const payroll = state.payroll?.settings || {};
  $("initialReserve").value = settings.initial_reserve ?? 0;
  $("forecastMonths").value = settings.forecast_months ?? 4;
  const morningReportTime = $("morningReportTime");
  if (morningReportTime) morningReportTime.value = settings.morning_report_time || "09:00";

  $("payrollEnabled").checked = Boolean(payroll.payroll_enabled);
  $("salaryGross").value = payroll.salary_gross ?? 0;
  $("bonusGross").value = payroll.bonus_gross ?? 0;
  $("taxRate").value = payroll.tax_rate ?? 13;
  $("salaryDay").value = payroll.salary_day ?? 7;
  $("advanceDay").value = payroll.advance_day ?? 22;

  const preview = state.payroll?.preview;
  const previewEl = $("payrollPreview");
  if (preview) {
    const advShift = preview.advance_date !== preview.advance_nominal_date ? ` · перенос с ${fmtDate(preview.advance_nominal_date)}` : '';
    const salShift = preview.salary_date !== preview.salary_nominal_date ? ` · перенос с ${fmtDate(preview.salary_nominal_date)}` : '';
    const vacationLine = preview.vacation_workdays > 0
      ? `<div class="preview-line"><span>Рабочих дней отпуска</span><strong>${preview.vacation_workdays}</strong></div>` : '';
    previewEl.innerHTML = `
      <div class="preview-title">Расчёт за ${fmtMonth(preview.accrual_year, preview.accrual_month)}</div>
      <div class="preview-line"><span>Отработано 1–15</span><strong>${preview.worked_days_first_half} из ${preview.workdays_first_half}</strong></div>
      ${vacationLine}
      <div class="preview-line"><span>Оклад за отработанные дни</span><strong>${money(preview.salary_net_for_worked_days)}</strong></div>
      <div class="preview-line"><span>Аванс · ${fmtDate(preview.advance_date)}${advShift}</span><strong>${money(preview.advance)}</strong></div>
      <div class="preview-line"><span>Зарплата + премия · ${fmtDate(preview.salary_date)}${salShift}</span><strong>${money(preview.final_salary)}</strong></div>
      <div class="row-sub">Полный чистый оклад ${money(preview.salary_net)} · премия после НДФЛ ${money(preview.bonus_net)}. Отпускные учитываются отдельно.</div>`;
  } else {
    previewEl.innerHTML = `<div class="empty">Включите автоматический расчёт и сохраните оклад, чтобы увидеть сумму аванса и зарплаты.</div>`;
  }

  const payrollChanges = state.payroll?.changes || [];
  const payrollChangesList = $("payrollChangesList");
  // Desktop Telegram can briefly retain an older HTML shell after an update.
  if (payrollChangesList) payrollChangesList.innerHTML = payrollChanges.length ? payrollChanges.map(change => {
    const [year, month] = change.effective_month.split("-").map(Number);
    return `<div class="list-row"><div class="row-text"><div class="row-title">С ${fmtMonth(year, month)}</div><div class="row-sub">Оклад ${money(change.salary_gross)} · премия ${money(change.bonus_gross)} до НДФЛ</div></div><div class="actions"><button class="tiny" onclick="editPayrollChange(${change.id})">Изм.</button><button class="tiny danger" onclick="deletePayrollChange(${change.id})">×</button></div></div>`;
  }).join("") : `<div class="empty">Будущих изменений пока нет.</div>`;

  $("vacationsList").innerHTML = state.vacations.length ? state.vacations.map(v => `
    <div class="list-row"><div class="row-text"><div class="row-title">Отпуск ${fmtDate(v.start_date)} — ${fmtDate(v.end_date)}</div><div class="row-sub">Выплата ${fmtDate(v.payment_date)}${v.note ? ` · ${escapeHtml(v.note)}` : ''}</div></div><div><div class="amount income">${money(v.amount)}</div><div class="actions"><button class="tiny" onclick="editVacation(${v.id})">Изм.</button><button class="tiny danger" onclick="deleteVacation(${v.id})">×</button></div></div></div>`).join("") : `<div class="empty">Отпуска пока не добавлены.</div>`;

  $("billRulesList").innerHTML = b.bill_rules.length ? b.bill_rules.map(r => `<div class="list-row"><div class="row-text"><div class="row-title"><i class="status-dot ${r.active ? "on" : "off"}" title="${r.active ? "Включён" : "Выключен"}" aria-label="${r.active ? "Включён" : "Выключен"}"></i>${escapeHtml(r.title)} · ${r.once_date ? fmtDate(r.once_date) : r.month_of_year ? `${r.day_of_month} ${MONTHS_GENITIVE[r.month_of_year - 1]}` : `${r.day_of_month} числа`}</div><div class="row-sub">${!r.active ? "выключен · не учитывается в бюджете и аналитике" : r.once_date ? "разово · погашение кредитки" : r.month_of_year ? "раз в год" : "ежемесячно"}</div></div><div><div class="amount ${r.active ? "expense" : "muted"}">${money(r.amount)}</div><div class="actions"><button class="tiny" onclick="editBillRule(${r.id})">Изм.</button><button class="tiny danger" onclick="deleteBillRule(${r.id})">×</button></div></div></div>`).join("") : `<div class="empty">Добавьте аренду, кредиты, подписки и другие обязательные платежи.</div>`;
  renderCategories();
}

function renderCategories() {
  const categories = state.bootstrap?.categories || [];
  $("categoriesList").innerHTML = categories.map(category => `
    <div class="category-order-item" data-category-id="${category.id}">
      <button class="category-order-name" type="button" data-edit-category="${category.id}" aria-label="Изменить ${escapeHtml(category.title)}"><b>${escapeHtml(category.emoji)}</b><span>${escapeHtml(category.title)}</span><i aria-hidden="true">✎</i></button>
      <button class="category-drag-handle" type="button" aria-label="Перетащить ${escapeHtml(category.title)}" title="Зажмите и перетащите">☰</button>
    </div>`).join("");
}

function openCategoryDialog(category = null) {
  // Desktop Telegram can briefly retain an older HTML shell after an update.
  if (!$("categoryDialog")) return toast("Обновите приложение, чтобы изменить категорию");
  const categories = state.bootstrap?.categories || [];
  $("categoryDialogTitle").textContent = category ? "Категория" : "Новая категория";
  $("categoryId").value = category?.id || "";
  $("categoryEmoji").value = category?.emoji || "💳";
  $("categoryTitle").value = category?.title || "";
  const others = categories.filter(c => c.id !== category?.id);
  $("categoryMoveTo").innerHTML = others.map(c => `<option value="${c.id}">Перенести в ${escapeHtml(c.emoji)} ${escapeHtml(c.title)}</option>`).join("")
    + `<option value="">Оставить без категории</option>`;
  $("categoryDeleteBlock").style.display = category && others.length ? "" : "none";
  $("categoryDialog").showModal();
}

$("categoriesList").addEventListener("click", event => {
  const button = event.target.closest?.("[data-edit-category]");
  if (!button) return;
  const id = Number(button.dataset.editCategory);
  const category = (state.bootstrap?.categories || []).find(c => c.id === id);
  if (category) openCategoryDialog(category);
});

$("addCategoryBtn").addEventListener("click", () => openCategoryDialog());

$("categoryForm")?.addEventListener("submit", async e => {
  e.preventDefault();
  const id = $("categoryId").value;
  const body = { title: $("categoryTitle").value.trim(), emoji: $("categoryEmoji").value.trim() || "💳" };
  try {
    await api(id ? `/api/categories/${id}` : "/api/categories", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
    $("categoryDialog").close();
    toast(id ? "Категория сохранена" : "Категория добавлена");
    await loadAll();
  } catch (err) { toast(err.message); }
});

$("deleteCategoryBtn")?.addEventListener("click", async () => {
  const id = $("categoryId").value;
  if (!id || !confirm(`Удалить категорию «${$("categoryTitle").value}»?`)) return;
  const moveTo = $("categoryMoveTo").value;
  try {
    await api(`/api/categories/${id}${moveTo ? `?move_to=${moveTo}` : ""}`, { method: "DELETE" });
    $("categoryDialog").close();
    toast("Категория удалена");
    await loadAll();
  } catch (err) { toast(err.message); }
});

let categoryDrag = null;

async function persistCategoryOrder(previous, categoryIds) {
  const byId = new Map(previous.map(category => [category.id, category]));
  const categories = categoryIds.map(id => byId.get(id)).filter(Boolean);
  if (categories.length !== previous.length) {
    renderCategories();
    return;
  }
  state.bootstrap.categories = categories;
  fillCategorySelects();
  try {
    await api('/api/categories/order', {
      method: 'PUT', body: JSON.stringify({category_ids: categoryIds})
    });
  } catch (error) {
    state.bootstrap.categories = previous;
    renderCategories();
    fillCategorySelects();
    toast(error.message);
  }
}

function categoryIdsFromList(list) {
  return [...list.querySelectorAll('.category-order-item')]
    .map(item => Number(item.dataset.categoryId));
}

const categoriesList = $("categoriesList");
const CATEGORY_MOVE_MS = 200;

function reducedMotion() {
  return Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
}

// FLIP: the items jump to their new place in the DOM, then slide there from
// where they were drawn a moment ago.
function slideItems(items, change) {
  const animate = !reducedMotion() && items.every(item => item.animate);
  const before = animate ? items.map(item => item.getBoundingClientRect().top) : [];
  change();
  if (!animate) return;
  items.forEach((item, index) => {
    item.categoryMove?.cancel();
    const shift = before[index] - item.getBoundingClientRect().top;
    if (!shift) return;
    item.categoryMove = item.animate(
      [{transform: `translateY(${shift}px)`}, {transform: 'none'}],
      {duration: CATEGORY_MOVE_MS, easing: 'cubic-bezier(.2,.8,.2,1)'}
    );
  });
}

function placeDraggedCategory(clientY) {
  const {item} = categoryDrag;
  const top = clientY - categoriesList.getBoundingClientRect().top - categoryDrag.grabOffset;
  item.style.transform = `translateY(${top - item.offsetTop}px)`;
}

function beginCategoryDrag(target, inputId, clientY) {
  if (categoryDrag) return false;
  const handle = target.closest('.category-drag-handle');
  if (!handle) return false;
  const item = handle.closest('.category-order-item');
  if (!item) return false;
  item.categoryMove?.cancel();
  categoryDrag = {
    inputId,
    item,
    previous: [...(state.bootstrap?.categories || [])],
    grabOffset: clientY - categoriesList.getBoundingClientRect().top - item.offsetTop,
  };
  item.classList.add('dragging');
  categoriesList.classList.add('sorting');
  return true;
}

function moveCategoryDrag(clientY, inputId) {
  if (!categoryDrag || categoryDrag.inputId !== inputId) return false;
  const {item} = categoryDrag;
  // Compare layout positions (offsetTop ignores the sliding transforms), so
  // neighbours mid-animation do not make the order flicker.
  const center = clientY - categoriesList.getBoundingClientRect().top - categoryDrag.grabOffset + item.offsetHeight / 2;
  const others = [...categoriesList.querySelectorAll('.category-order-item:not(.dragging)')];
  const before = others.find(other => center < other.offsetTop + other.offsetHeight / 2) || null;
  if (item.nextElementSibling !== before) {
    slideItems(others, () => categoriesList.insertBefore(item, before));
  }
  placeDraggedCategory(clientY);
  if (clientY < 90) window.scrollBy(0, -12);
  else if (clientY > window.innerHeight - 90) window.scrollBy(0, 12);
  return true;
}

async function finishCategoryDrag(inputId, cancelled = false) {
  if (!categoryDrag || categoryDrag.inputId !== inputId) return;
  const drag = categoryDrag;
  categoryDrag = null;
  categoriesList.classList.remove('sorting');
  if (cancelled) {
    drag.item.classList.remove('dragging');
    renderCategories();
    return;
  }
  // Settle the dragged item into its slot instead of snapping.
  slideItems([drag.item], () => { drag.item.style.transform = ''; });
  if (drag.item.categoryMove) drag.item.categoryMove.onfinish = () => drag.item.classList.remove('dragging');
  else drag.item.classList.remove('dragging');
  const categoryIds = categoryIdsFromList(categoriesList);
  if (categoryIds.every((id, index) => id === drag.previous[index]?.id)) return;
  await persistCategoryOrder(drag.previous, categoryIds);
}

categoriesList.addEventListener('pointerdown', event => {
  // Touch is handled below through the iOS-compatible touch event path.
  if (event.pointerType === 'touch') return;
  const inputId = `pointer:${event.pointerId}`;
  if (!beginCategoryDrag(event.target, inputId, event.clientY)) return;
  event.preventDefault();
  categoriesList.setPointerCapture?.(event.pointerId);
});

categoriesList.addEventListener('pointermove', event => {
  if (!moveCategoryDrag(event.clientY, `pointer:${event.pointerId}`)) return;
  event.preventDefault();
});

categoriesList.addEventListener('pointerup', event => {
  const inputId = `pointer:${event.pointerId}`;
  if (!categoryDrag || categoryDrag.inputId !== inputId) return;
  try { categoriesList.releasePointerCapture?.(event.pointerId); } catch (_) {}
  finishCategoryDrag(inputId);
});
categoriesList.addEventListener('pointercancel', event => {
  finishCategoryDrag(`pointer:${event.pointerId}`, true);
});

// Telegram's iOS webview can omit Pointer Events while still delivering
// classic touch events. Keep a dedicated non-passive fallback for phones.
categoriesList.addEventListener('touchstart', event => {
  if (categoryDrag) return;
  const touch = event.changedTouches[0];
  if (!touch || !beginCategoryDrag(event.target, `touch:${touch.identifier}`, touch.clientY)) return;
  event.preventDefault();
}, {passive:false});

categoriesList.addEventListener('touchmove', event => {
  if (!categoryDrag || !categoryDrag.inputId.startsWith('touch:')) return;
  const identifier = Number(categoryDrag.inputId.slice(6));
  const touch = [...event.touches].find(item => item.identifier === identifier);
  if (!touch || !moveCategoryDrag(touch.clientY, categoryDrag.inputId)) return;
  event.preventDefault();
}, {passive:false});

categoriesList.addEventListener('touchend', event => {
  if (!categoryDrag || !categoryDrag.inputId.startsWith('touch:')) return;
  const identifier = Number(categoryDrag.inputId.slice(6));
  if (![...event.changedTouches].some(item => item.identifier === identifier)) return;
  finishCategoryDrag(categoryDrag.inputId);
});
categoriesList.addEventListener('touchcancel', () => {
  if (categoryDrag?.inputId.startsWith('touch:')) finishCategoryDrag(categoryDrag.inputId, true);
});

function fillCategorySelects() {
  const cats = state.bootstrap?.categories || [];
  const options = cats.map(c => `<option value="${c.id}">${escapeHtml(c.emoji)} ${escapeHtml(c.title)}</option>`).join("");
  $("expenseCategory").innerHTML = options;
  $("ruleBillCategory").innerHTML = `<option value="">Без категории</option>${options}`;
}

function escapeHtml(s) { return String(s ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }

const RESERVE_PAGES = ['piggy', 'plan', 'reserve', 'obligations'];
function switchPage(page) {
  // The buffer belongs to the payroll mode; with side jobs "Резервы" opens the reserve.
  if (page === 'buffer' && isIrregular()) page = 'reserve';
  document.querySelectorAll('.page').forEach(x => x.classList.toggle('active', x.dataset.page === page));
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.nav === (RESERVE_PAGES.includes(page) ? 'buffer' : page)));
  window.budgetDesign?.navigation(page);
  window.scrollTo({top:0, behavior:'smooth'});
}

document.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', () => switchPage(btn.dataset.nav)));
$("refreshBtn").addEventListener("click", () => loadAll());
let editingTransactionId = null;

// Several receipts in one expense: "+" moves the typed amount into a list of parts,
// and the expense is saved once with their total (kopecks avoid float drift).
let expenseParts = [];
const toKopecks = value => Math.round((window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(value) : Number(value) || 0) * 100);
const kopecksText = k => new Intl.NumberFormat("ru-RU", { minimumFractionDigits: k % 100 ? 2 : 0, maximumFractionDigits: 2 }).format(k / 100);

function renderExpenseParts() {
  const box = $("expenseParts"), input = $("expenseAmount");
  if (!box || !input) return;
  input.required = !expenseParts.length;
  box.classList.toggle("hidden", !expenseParts.length);
  if (!expenseParts.length) { box.innerHTML = ""; return; }
  const total = expenseParts.reduce((sum, k) => sum + k, 0) + Math.max(toKopecks(input.value), 0);
  box.innerHTML = expenseParts.map((k, i) => `<button type="button" class="sum-part" data-part="${i}" aria-label="Убрать ${kopecksText(k)} ₽">${kopecksText(k)}<i aria-hidden="true">×</i></button>`).join('<span class="sum-plus" aria-hidden="true">+</span>')
    + `<span class="sum-total">Итого ${money(total / 100)}</span>`;
}

function resetExpenseParts() { expenseParts = []; renderExpenseParts(); }

function addExpensePart(raw) {
  const k = toKopecks(raw);
  if (k <= 0) return false;
  expenseParts.push(k);
  return true;
}

function addTypedExpensePart() {
  const input = $("expenseAmount");
  if (!addExpensePart(input.value)) { toast("Введите сумму, потом нажмите +"); input.focus(); return; }
  input.value = ""; renderExpenseParts(); input.focus();
}

$("expenseAddPart")?.addEventListener("click", addTypedExpensePart);
// Keep the keyboard open: tapping "+" must not take focus away from the amount field.
["pointerdown", "mousedown"].forEach(type => $("expenseAddPart")?.addEventListener(type, e => e.preventDefault()));
$("expenseParts")?.addEventListener("click", e => {
  const part = e.target.closest?.("[data-part]");
  if (!part) return;
  expenseParts.splice(Number(part.dataset.part), 1);
  renderExpenseParts();
});
// Keyboards that have "+" (desktop, some Android ones) work like the button;
// "=305+589+102", typed or pasted, is split the same way. Runs before money-input.js strips the signs.
$("expenseAmount")?.addEventListener("input", e => {
  const input = e.target;
  if (/[+=]/.test(input.value)) {
    const pieces = input.value.replace(/=/g, "").split("+");
    const last = pieces.pop();
    pieces.forEach(addExpensePart);
    input.value = last;
  }
  renderExpenseParts();
});

function expenseAmountTotal() {
  return (expenseParts.reduce((sum, k) => sum + k, 0) + Math.max(toKopecks($("expenseAmount").value), 0)) / 100;
}

function expenseNoteWithParts() {
  const note = $("expenseNote").value;
  if (note.trim() || !expenseParts.length) return note;
  const last = toKopecks($("expenseAmount").value);
  return [...expenseParts, ...(last > 0 ? [last] : [])].map(kopecksText).join(" + ").slice(0, 200);
}

function openExpenseDialog() {
  editingTransactionId = null; $("expenseForm").reset(); $("expenseDate").value = todayISO();
  resetExpenseParts();
  window.accountsUi?.prepareExpense();
  $("expenseDialog").showModal(); setTimeout(() => $("expenseAmount").focus(), 50);
}
function openIncomeDialog() {
  if (isIrregular() && window.irregularUi) { window.irregularUi.openIncome(); return; }
  editingTransactionId = null; $("incomeTxForm").reset(); $("incomeTxDate").value = todayISO();
  window.accountsUi?.prepareIncome("incomeTx");
  $("incomeTxDialog").showModal();
}
["quickExpenseBtn", "navAddExpense"].forEach(id => $(id)?.addEventListener("click", openExpenseDialog));
["addIncomeTxBtn", "quickIncomeBtn"].forEach(id => $(id)?.addEventListener("click", openIncomeDialog));
// "В копилку" on the home screen is the same card → piggy transfer as on the reserves page.
$("quickPiggyBtn")?.addEventListener("click", () => $("piggyTransferBtn")?.click());

$("txSearch")?.addEventListener("input", () => renderTransactions());
document.querySelectorAll("[data-txf]").forEach(btn => btn.addEventListener("click", () => {
  transactionFilter = btn.dataset.txf;
  document.querySelectorAll("[data-txf]").forEach(b => b.setAttribute("aria-pressed", String(b === btn)));
  renderTransactions();
}));
// «Сегодня» and «Период» exclude each other; a second tap turns the chip off.
// «Период» always opens on the current period.
function toggleTransactionRange(range) {
  transactionRange = transactionRange === range ? "all" : range;
  transactionPeriod = null;
  $("txToday")?.setAttribute("aria-pressed", String(transactionRange === "today"));
  $("txPeriod")?.setAttribute("aria-pressed", String(transactionRange === "period"));
  renderTransactions();
  if (transactionRange === "period") coverTransactionPeriod();
}
$("txToday")?.addEventListener("click", () => toggleTransactionRange("today"));
$("txPeriod")?.addEventListener("click", () => toggleTransactionRange("period"));

// ‹ › step through card periods: their bounds come from the paydays, which
// only the server knows; › stops at the current period.
let transactionPeriodStep = null;
function stepTransactionPeriod(direction) {
  const current = state.dashboard?.period;
  const shown = transactionPeriod || current;
  if (!current || transactionPeriodStep) return transactionPeriodStep;
  const day = direction < 0 ? shiftISODate(shown.start, -1) : shiftISODate(shown.end, 1);
  if (day >= current.start) {
    transactionPeriod = null;
    renderTransactions();
    return Promise.resolve();
  }
  transactionPeriodStep = api(`/api/card-period?day=${day}`)
    .then(period => {
      transactionPeriod = period;
      renderTransactions();
      return coverTransactionPeriod();
    })
    .catch(e => toast(e.message))
    .finally(() => { transactionPeriodStep = null; });
  return transactionPeriodStep;
}
$("txPeriodPrev")?.addEventListener("click", () => stepTransactionPeriod(-1));
$("txPeriodNext")?.addEventListener("click", () => stepTransactionPeriod(1));
document.querySelectorAll("[data-txview]").forEach(btn => btn.addEventListener("click", () => {
  transactionView = btn.dataset.txview;
  document.querySelectorAll("[data-txview]").forEach(b => b.setAttribute("aria-selected", String(b === btn)));
  $("txChips")?.classList.toggle("hidden", transactionView === "bills");
  const search = $("txSearch");
  if (search) search.placeholder = transactionView === "bills" ? "Поиск по платежам" : "Поиск по операциям";
  renderTransactions();
}));

window.editTransaction = id => {
  const transaction = state.transactions.find(item => item.id === id && !item.bill_rule_id);
  if (!transaction) return;
  if (transaction.type === "income" && isIrregular() && window.irregularUi) {
    window.irregularUi.openIncome(transaction);
    return;
  }
  editingTransactionId = id;
  if (transaction.type === "expense") {
    resetExpenseParts();
    $("expenseAmount").value = transaction.amount;
    $("expenseDate").value = transaction.tx_date;
    $("expenseCategory").value = transaction.category_id || "";
    $("expenseNote").value = transaction.note || "";
    window.accountsUi?.prepareExpense(transaction);
    $("expenseDialog").showModal();
  } else {
    $("incomeTxAmount").value = transaction.amount;
    $("incomeTxDate").value = transaction.tx_date;
    $("incomeTxDestination").value = transaction.income_destination || "daily";
    $("incomeTxNote").value = transaction.note || "";
    window.accountsUi?.prepareIncome("incomeTx", transaction);
    $("incomeTxDialog").showModal();
  }
};

$("expenseForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const path = editingTransactionId ? `/api/transactions/${editingTransactionId}` : "/api/transactions";
    await api(path, { method:editingTransactionId ? "PUT" : "POST", body: JSON.stringify({ type:"expense", amount:expenseAmountTotal(), tx_date:$("expenseDate").value, category_id:Number($("expenseCategory").value), note:expenseNoteWithParts(), ...(window.accountsUi?.expensePayload() || {}) }) });
    editingTransactionId = null;
    $("expenseDialog").close(); e.target.reset(); resetExpenseParts(); toast("Расход записан"); await loadAll();
  } catch(e2) { toast(e2.message); }
});

$("incomeTxForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const path = editingTransactionId ? `/api/transactions/${editingTransactionId}` : "/api/transactions";
    await api(path, { method:editingTransactionId ? "PUT" : "POST", body: JSON.stringify({ type:"income", amount:moneyValue("incomeTxAmount"), tx_date:$("incomeTxDate").value, category_id:null, note:$("incomeTxNote").value, income_destination:$("incomeTxDestination").value, ...(window.accountsUi?.fieldPayload("incomeTx") || {}) }) });
    editingTransactionId = null;
    $("incomeTxDialog").close(); e.target.reset(); toast("Доход записан"); await loadAll();
  } catch(e2) { toast(e2.message); }
});

window.deleteTx = async (id) => { if (!confirm("Удалить операцию?")) return; try { await api(`/api/transactions/${id}`, {method:"DELETE"}); await loadAll(); } catch(e) { toast(e.message); } };
window.payBill = async (id, due) => { try { await api(`/api/bills/${id}/pay`, {method:"POST", body:JSON.stringify({due_date:due})}); toast("Отмечено оплачено"); await loadAll(); } catch(e){ toast(e.message); } };
window.editBillPayment = paymentId => {
  const payment = state.plan.find(item => item.payment_id === paymentId)
    || state.transactions.find(item => item.id === paymentId && item.bill_rule_id);
  if (!payment) return;
  $("billPaymentId").value = paymentId;
  $("billPaymentAmount").value = payment.amount;
  $("billRemainderDestination").value = payment.remainder_destination || "budget";
  const planned = payment.planned_amount ?? payment.bill_planned_amount ?? payment.amount;
  $("billPaymentPlanned").textContent = `Запланировано: ${money(planned)}`;
  $("billPaymentDialog").showModal();
};

$("billPaymentForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const paymentId = Number($("billPaymentId").value);
  try {
    await api(`/api/bill-payments/${paymentId}`, {
      method: "PUT",
      body: JSON.stringify({
        amount: window.ruMoneyInput?.parseMoney($("billPaymentAmount").value) ?? Number($("billPaymentAmount").value),
        remainder_destination: $("billRemainderDestination").value,
      }),
    });
    $("billPaymentDialog").close();
    toast("Фактическая сумма сохранена");
    await loadAll();
  } catch (e2) { toast(e2.message); }
});

function openVacation(vacation = null) {
  const start = vacation?.start_date || todayISO();
  $("vacationId").value = vacation?.id || "";
  $("vacationDialogTitle").textContent = vacation ? "Изменить отпуск" : "Новый отпуск";
  $("vacationStart").value = start;
  $("vacationEnd").value = vacation?.end_date || start;
  $("vacationAmount").value = vacation?.amount ?? "";
  $("vacationPaymentDate").value = vacation?.payment_date || shiftISODate(start, -3);
  $("vacationNote").value = vacation?.note || "";
  $("vacationDialog").showModal();
}

$("addVacationBtn").addEventListener("click", () => openVacation());
$("vacationStart").addEventListener("change", () => {
  if (!$("vacationId").value) $("vacationPaymentDate").value = shiftISODate($("vacationStart").value, -3);
  if (!$("vacationEnd").value || $("vacationEnd").value < $("vacationStart").value) $("vacationEnd").value = $("vacationStart").value;
});
window.editVacation = id => openVacation(state.vacations.find(x => x.id === id));
window.deleteVacation = async id => {
  if (!confirm("Удалить отпуск?")) return;
  try { await api(`/api/vacations/${id}`, {method:"DELETE"}); toast("Отпуск удалён"); await loadAll(); } catch(e) { toast(e.message); }
};

$("vacationForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("vacationId").value;
  const body = {
    start_date: $("vacationStart").value,
    end_date: $("vacationEnd").value,
    amount: moneyValue("vacationAmount"),
    payment_date: $("vacationPaymentDate").value,
    note: $("vacationNote").value,
  };
  try {
    await api(id ? `/api/vacations/${id}` : "/api/vacations", {method:id?"PUT":"POST", body:JSON.stringify(body)});
    $("vacationDialog").close();
    toast("Отпуск сохранён");
    await loadAll();
  } catch(err) { toast(err.message); }
});

function openRule(mode, rule = null) {
  $("ruleMode").value = mode; $("ruleId").value = rule?.id || "";
  const income = mode === "income";
  $("ruleDialogTitle").textContent = rule ? "Изменить правило" : (income ? "Новый доход" : "Новый платеж");
  $("incomeRuleFields").style.display = income ? "block" : "none";
  $("billCategoryWrap").style.display = income ? "none" : "flex";
  // A yearly bill is due on its day of one month; a one-off (credit card)
  // bill keeps its own date and has no periodicity to choose.
  $("billPeriodWrap").style.display = income || rule?.once_date ? "none" : "flex";
  $("ruleBillPeriod").value = rule?.month_of_year ? "yearly" : "monthly";
  $("ruleBillMonth").value = String(rule?.month_of_year || new Date().getMonth() + 1);
  syncBillPeriod();
  $("ruleTitle").value = rule?.title || ""; $("ruleAmount").value = rule?.amount ?? ""; $("ruleDay").value = rule?.day_of_month ?? "";
  $("ruleKind").value = rule?.kind || "other"; $("ruleIsPayday").checked = Boolean(rule?.is_payday);
  $("ruleActive").checked = rule?.active !== 0 && rule?.active !== false;
  $("ruleEffectiveDate").value = todayISO();
  $("ruleBillCategory").value = rule?.category_id || "";
  $("ruleDialog").showModal();
}
function syncBillPeriod() {
  const yearly = $("ruleMode").value === "bill" && $("ruleBillPeriod").value === "yearly";
  $("billMonthWrap").style.display = yearly ? "flex" : "none";
}
$("ruleBillPeriod").addEventListener("change", syncBillPeriod);
$("addBillRuleBtn").addEventListener("click", () => openRule("bill"));
window.editBillRule = id => openRule("bill", state.bootstrap.bill_rules.find(x => x.id === id));
window.deleteBillRule = async id => { if(!confirm("Удалить платеж?")) return; try { await api(`/api/bill-rules/${id}`, {method:"DELETE"}); await loadAll(); } catch(e) { toast(e.message); } };

$("ruleForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = $("ruleMode").value, id = $("ruleId").value;
  let body;
  if (mode === "income") body = { title:$("ruleTitle").value, amount:moneyValue("ruleAmount"), day_of_month:Number($("ruleDay").value), kind:$("ruleKind").value, is_payday:$("ruleIsPayday").checked, active:$("ruleActive").checked, effective_date:$("ruleEffectiveDate").value };
  else body = { title:$("ruleTitle").value, amount:moneyValue("ruleAmount"), day_of_month:Number($("ruleDay").value), month_of_year:$("ruleBillPeriod").value === "yearly" ? Number($("ruleBillMonth").value) : null, category_id:$("ruleBillCategory").value ? Number($("ruleBillCategory").value) : null, active:$("ruleActive").checked, effective_date:$("ruleEffectiveDate").value };
  const base = mode === "income" ? "/api/income-rules" : "/api/bill-rules";
  try { await api(id ? `${base}/${id}` : base, {method:id?"PUT":"POST", body:JSON.stringify(body)}); $("ruleDialog").close(); toast("Сохранено"); await loadAll(); } catch(err){ toast(err.message); }
});

function generalSettingsPayload() {
  return {
    currency: "RUB",
    // The legacy reserve is no longer the cash-flow start capital.  Preserve
    // it here; the dedicated button below saves the visible start amount.
    initial_reserve: Number(state.bootstrap?.settings?.initial_reserve || 0),
    forecast_months: Number($("forecastMonths").value || 4),
    morning_report_time: $("morningReportTime")?.value || state.bootstrap?.settings?.morning_report_time || "09:00",
  };
}

function payrollSettingsPayload() {
  return {
    payroll_enabled: $("payrollEnabled").checked,
    salary_gross: moneyValue("salaryGross"),
    bonus_gross: moneyValue("bonusGross"),
    tax_rate: Number($("taxRate").value || 0),
    salary_day: Number($("salaryDay").value || 7),
    advance_day: Number($("advanceDay").value || 22),
  };
}

$("saveSettingsBtn").addEventListener("click", async () => {
  try {
    await api("/api/settings", {method:"PUT", body:JSON.stringify(generalSettingsPayload())});
    toast("Настройки сохранены");
    await loadAll();
  } catch(e) { toast(e.message); }
});

$("saveReminderSettingsBtn").addEventListener("click", async () => {
  try {
    await api("/api/settings", {method:"PUT", body:JSON.stringify(generalSettingsPayload())});
    toast("Уведомления сохранены");
    await loadAll();
  } catch(e) { toast(e.message); }
});

$("openPayrollChangeBtn")?.addEventListener("click", () => $("addPayrollChangeBtn")?.click());

$("addPayrollChangeBtn")?.addEventListener("click", () => {
  $("payrollChangeId").value = "";
  $("payrollChangeDialogTitle").textContent = "Запланировать изменение зарплаты";
  $("savePayrollChangeBtn").textContent = "Сохранить и пересчитать прогноз";
  $("payrollChangeMonth").value = "";
  $("payrollChangeSalary").value = $("salaryGross").value || "";
  $("payrollChangeBonus").value = $("bonusGross").value || "";
  $("payrollChangeDialog").showModal();
});

window.editPayrollChange = id => {
  const change = (state.payroll?.changes || []).find(item => item.id === id);
  if (!change) return;
  $("payrollChangeId").value = change.id;
  $("payrollChangeDialogTitle").textContent = "Изменить запланированную зарплату";
  $("savePayrollChangeBtn").textContent = "Сохранить изменения";
  $("payrollChangeMonth").value = change.effective_month.slice(0, 7);
  $("payrollChangeSalary").value = change.salary_gross;
  $("payrollChangeBonus").value = change.bonus_gross;
  $("payrollChangeDialog").showModal();
};

window.deletePayrollChange = async id => {
  if (!confirm("Удалить запланированное изменение?")) return;
  try {
    await api(`/api/payroll-changes/${id}`, { method:"DELETE" });
    await loadAll();
  } catch (e) { toast(e.message); }
};

$("payrollChangeForm")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const month = $("payrollChangeMonth").value;
    const changeId = $("payrollChangeId").value;
    const path = changeId ? "/api/payroll-changes/" + changeId : "/api/payroll-changes";
    await api(path, { method: changeId ? "PUT" : "POST", body:JSON.stringify({
      effective_month: `${month}-01`,
      salary_gross: moneyValue("payrollChangeSalary"),
      bonus_gross: moneyValue("payrollChangeBonus"),
    }) });
    $("payrollChangeDialog").close();
    toast(changeId ? "Запланированная зарплата изменена" : "Повышение запланировано: прогноз пересчитан");
    await loadAll();
  } catch (err) { toast(err.message); }
});

$("savePayrollBtn").addEventListener("click", async () => {
  try {
    await api("/api/payroll-settings", {method:"PUT", body:JSON.stringify(payrollSettingsPayload())});
    toast("Расчёт зарплаты сохранён");
    await loadAll();
  } catch(e) { toast(e.message); }
});

window.addEventListener("DOMContentLoaded", () => loadAll(), { once: true });
