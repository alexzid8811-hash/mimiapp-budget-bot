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

async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: requestHeaders(options.headers) });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Ошибка ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

function currency() { return state.bootstrap?.settings?.currency || "RUB"; }
function money(v) {
  return new Intl.NumberFormat("ru-RU", { style: "currency", currency: currency(), maximumFractionDigits: 2 }).format(Number(v || 0));
}
function moneyValue(id) {
  const value = $(id)?.value || "0";
  return window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(value) : Number(value) || 0;
}
function fmtDate(s) { return new Intl.DateTimeFormat("ru-RU", { day:"numeric", month:"short" }).format(new Date(`${s}T12:00:00`)); }
function fmtMonth(year, month) { return new Intl.DateTimeFormat("ru-RU", { month:"long", year:"numeric" }).format(new Date(year, month - 1, 1)); }
function todayISO() { return window.budgetDate.today(); }
function shiftISODate(iso, days) { return window.budgetDate.shift(iso, days); }

let loadVersion = 0;

async function loadAll() {
  const version = ++loadVersion;
  try {
    const [bootstrap, payroll, vacations, dashboard, transactions, plan, cashflowSettings, cashflow] = await Promise.all([
      api("/api/bootstrap"), api("/api/payroll-settings"), api("/api/vacations"), api("/api/dashboard"), api("/api/transactions"), api("/api/plan"), api("/api/cashflow-settings"), api("/api/cashflow")
    ]);
    if (version !== loadVersion) return;
    window.budgetDate.configure(bootstrap.budget_timezone);
    state = { bootstrap, payroll, vacations, dashboard, transactions, plan, cashflowSettings, cashflow };
    render();
    window.applyCashflowSettings?.(cashflowSettings);
    window.applyCashflow?.(cashflow);
    await window.refreshBuffer?.(cashflow);
    window.budgetDesign?.render(state);
  } catch (e) { toast(e.message); }
}

function render() {
  renderHeader(); renderDashboard(); renderTransactions(); renderPlan(); renderSettings(); fillCategorySelects();
}

function renderHeader() {
  const name = state.bootstrap?.user?.first_name;
  $("hello").textContent = name ? `Бюджет, ${name}` : "Мой бюджет";
}

function renderDashboard() {
  const d = state.dashboard; if (!d) return;
  $("dailyAvailable").textContent = money(d.daily_available);
  $("periodCaption").textContent = `${fmtDate(d.period.start)} — ${fmtDate(d.period.end)} · ${d.period.days_left} дн.`;
  $("spentToday").textContent = money(d.spent_today);
  // Fallback values until the detailed cash-flow snapshot loads.
  $("cardCash").textContent = money(d.remaining);
  $("cardBuffer").textContent = money(d.reserve.balance);
  $("cardPiggy").textContent = "—";
  $("cardReservesTotal").textContent = money(d.reserve.balance);
  $("reserveTarget").textContent = `цель ${money(d.reserve.future_target)}`;
  const movement = Number(d.reserve.auto_movement || 0);
  $("reserveReason").textContent = movement > 0
    ? `${d.reserve.reason}: ${money(movement)} автоматически исключено из свободного бюджета.`
    : movement < 0 ? `${d.reserve.reason}: ${money(Math.abs(movement))} возвращено из копилки в период.` : d.reserve.reason;
  const pct = d.period_budget > 0 ? Math.min(100, Math.max(0, d.spent / d.period_budget * 100)) : 0;
  $("dayProgress").style.width = `${pct}%`;
  const warn = $("deficitWarning");
  if (d.deficit > 0) { warn.textContent = `Не хватает ${money(d.deficit)} даже после доступного резерва. Проверьте будущие платежи или доходы.`; warn.classList.remove("hidden"); }
  else warn.classList.add("hidden");
  $("forecastList").innerHTML = d.forecast.length ? d.forecast.map(p => `
    <div class="list-row"><div class="row-text"><div class="row-title">${fmtDate(p.start)} — ${fmtDate(p.end)}</div><div class="row-sub">Доход ${money(p.income)} · платежи ${money(p.mandatory)}</div></div><div class="amount ${p.net < 0 ? 'expense':'income'}">${p.net >= 0 ? '+' : ''}${money(p.net)}</div></div>`).join("") : `<div class="empty">Добавьте суммы зарплаты и аванса, чтобы увидеть прогноз.</div>`;
}

function renderTransactions() {
  const el = $("transactionsList");
  if (!state.transactions.length) { el.innerHTML = `<div class="empty">Пока нет операций.</div>`; return; }
  el.innerHTML = state.transactions.map(t => {
    const isExpense = t.type === "expense";
    const title = t.bill_title || t.note || t.category_title || (isExpense ? "Расход" : "Доход");
    const editPayment = t.bill_rule_id
      ? `<button class="tiny" onclick="editBillPayment(${t.id})">Изменить</button>`
      : `<button class="tiny" onclick="editTransaction(${t.id})">Изменить</button>`;
    return `<div class="list-row"><div class="row-main"><div class="emoji">${escapeHtml(t.category_emoji) || (isExpense ? '💳':'💰')}</div><div class="row-text"><div class="row-title">${escapeHtml(title)}</div><div class="row-sub">${fmtDate(t.tx_date)}${t.category_title ? ` · ${escapeHtml(t.category_title)}`:''}</div></div></div><div><div class="amount ${t.type}">${isExpense?'-':'+'}${money(t.amount)}</div><div class="actions">${editPayment}<button class="tiny danger" onclick="deleteTx(${t.id})">Удалить</button></div></div></div>`;
  }).join("");
}

function renderPlan() {
  const el = $("planList");
  if (!state.plan.length) { el.innerHTML = `<div class="empty">В этом периоде обязательных платежей нет.</div>`; return; }
  el.innerHTML = state.plan.map(p => {
    const planned = Number(p.planned_amount ?? p.amount);
    const paidCaption = Number(p.amount) === planned
      ? 'оплачено'
      : `оплачено из ${money(planned)}`;
    const remainderCaption = Number(p.remainder_amount || 0) > 0
      ? ` · ${money(p.remainder_amount)} в копилке`
      : '';
    const action = p.paid
      ? `<div class="row-sub">${paidCaption}${remainderCaption}</div><button class="tiny" onclick="editBillPayment(${p.payment_id})">Изменить</button>`
      : `<button class="tiny" onclick="payBill(${p.id},'${p.due_date}')">Оплачено</button>`;
    return `<div class="list-row ${p.paid?'paid':''}"><div class="row-main"><div class="emoji">${escapeHtml(p.category_emoji) || '📌'}</div><div class="row-text"><div class="row-title">${escapeHtml(p.title)}</div><div class="row-sub">${fmtDate(p.due_date)} · уже учтено в бюджете</div></div></div><div><div class="amount expense">${money(p.amount)}</div>${action}</div></div>`;
  }).join("");
}

function renderSettings() {
  const b = state.bootstrap; if (!b) return;
  const settings = b.settings || {};
  const payroll = state.payroll?.settings || {};
  $("initialReserve").value = settings.initial_reserve ?? 0;
  $("forecastMonths").value = settings.forecast_months ?? 4;
  $("currency").value = settings.currency || "RUB";

  $("payrollEnabled").checked = Boolean(payroll.payroll_enabled);
  $("salaryGross").value = payroll.salary_gross ?? 0;
  $("bonusGross").value = payroll.bonus_gross ?? 0;
  $("taxRate").value = payroll.tax_rate ?? 13;
  $("salaryDay").value = payroll.salary_day ?? 7;
  $("payrollEffectiveDate").value = todayISO();
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

  $("vacationsList").innerHTML = state.vacations.length ? state.vacations.map(v => `
    <div class="list-row"><div class="row-text"><div class="row-title">Отпуск ${fmtDate(v.start_date)} — ${fmtDate(v.end_date)}</div><div class="row-sub">Выплата ${fmtDate(v.payment_date)}${v.note ? ` · ${escapeHtml(v.note)}` : ''}</div></div><div><div class="amount income">${money(v.amount)}</div><div class="actions"><button class="tiny" onclick="editVacation(${v.id})">Изм.</button><button class="tiny danger" onclick="deleteVacation(${v.id})">×</button></div></div></div>`).join("") : `<div class="empty">Отпуска пока не добавлены.</div>`;

  const incomeRules = Boolean(payroll.payroll_enabled) ? b.income_rules.filter(r => r.kind === 'other') : b.income_rules;
  $("incomeRulesList").innerHTML = incomeRules.length ? incomeRules.map(r => `<div class="list-row"><div class="row-text"><div class="row-title">${escapeHtml(r.title)} · ${r.day_of_month} числа</div><div class="row-sub">${r.active ? 'активно' : 'выключено'}</div></div><div><div class="amount income">${money(r.amount)}</div><div class="actions"><button class="tiny" onclick="editIncomeRule(${r.id})">Изм.</button><button class="tiny danger" onclick="deleteIncomeRule(${r.id})">×</button></div></div></div>`).join("") : `<div class="empty">Дополнительных регулярных доходов пока нет.</div>`;
  $("billRulesList").innerHTML = b.bill_rules.length ? b.bill_rules.map(r => `<div class="list-row"><div class="row-text"><div class="row-title">${escapeHtml(r.title)} · ${r.day_of_month} числа</div><div class="row-sub">ежемесячно</div></div><div><div class="amount expense">${money(r.amount)}</div><div class="actions"><button class="tiny" onclick="editBillRule(${r.id})">Изм.</button><button class="tiny danger" onclick="deleteBillRule(${r.id})">×</button></div></div></div>`).join("") : `<div class="empty">Добавьте аренду, кредиты, подписки и другие обязательные платежи.</div>`;
  $("categoriesList").innerHTML = b.categories.map(c => `<span class="chip">${escapeHtml(c.emoji)} ${escapeHtml(c.title)}</span>`).join("");
}

function fillCategorySelects() {
  const cats = state.bootstrap?.categories || [];
  const options = cats.map(c => `<option value="${c.id}">${escapeHtml(c.emoji)} ${escapeHtml(c.title)}</option>`).join("");
  $("expenseCategory").innerHTML = options;
  $("ruleBillCategory").innerHTML = `<option value="">Без категории</option>${options}`;
}

function escapeHtml(s) { return String(s ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }

function switchPage(page) {
  document.querySelectorAll('.page').forEach(x => x.classList.toggle('active', x.dataset.page === page));
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.nav === (page === 'piggy' ? 'buffer' : page)));
  window.budgetDesign?.navigation(page);
  window.scrollTo({top:0, behavior:'smooth'});
}

document.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', () => switchPage(btn.dataset.nav)));
$("refreshBtn").addEventListener("click", () => loadAll());
let editingTransactionId = null;
$("quickExpenseBtn").addEventListener("click", () => {
  editingTransactionId = null; $("expenseForm").reset(); $("expenseDate").value = todayISO();
  $("expenseDialog").showModal(); setTimeout(() => $("expenseAmount").focus(), 50);
});
$("addIncomeTxBtn").addEventListener("click", () => {
  editingTransactionId = null; $("incomeTxForm").reset(); $("incomeTxDate").value = todayISO();
  $("incomeTxDialog").showModal();
});

window.editTransaction = id => {
  const transaction = state.transactions.find(item => item.id === id && !item.bill_rule_id);
  if (!transaction) return;
  editingTransactionId = id;
  if (transaction.type === "expense") {
    $("expenseAmount").value = transaction.amount;
    $("expenseDate").value = transaction.tx_date;
    $("expenseCategory").value = transaction.category_id || "";
    $("expenseNote").value = transaction.note || "";
    $("expenseDialog").showModal();
  } else {
    $("incomeTxAmount").value = transaction.amount;
    $("incomeTxDate").value = transaction.tx_date;
    $("incomeTxNote").value = transaction.note || "";
    $("incomeTxDialog").showModal();
  }
};

$("expenseForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const path = editingTransactionId ? `/api/transactions/${editingTransactionId}` : "/api/transactions";
    await api(path, { method:editingTransactionId ? "PUT" : "POST", body: JSON.stringify({ type:"expense", amount:moneyValue("expenseAmount"), tx_date:$("expenseDate").value, category_id:Number($("expenseCategory").value), note:$("expenseNote").value }) });
    editingTransactionId = null;
    $("expenseDialog").close(); e.target.reset(); toast("Расход записан"); await loadAll();
  } catch(e2) { toast(e2.message); }
});

$("incomeTxForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const path = editingTransactionId ? `/api/transactions/${editingTransactionId}` : "/api/transactions";
    await api(path, { method:editingTransactionId ? "PUT" : "POST", body: JSON.stringify({ type:"income", amount:moneyValue("incomeTxAmount"), tx_date:$("incomeTxDate").value, category_id:null, note:$("incomeTxNote").value }) });
    editingTransactionId = null;
    $("incomeTxDialog").close(); e.target.reset(); toast("Доход записан"); await loadAll();
  } catch(e2) { toast(e2.message); }
});

window.deleteTx = async (id) => { if (!confirm("Удалить операцию?")) return; await api(`/api/transactions/${id}`, {method:"DELETE"}); await loadAll(); };
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
  $("ruleTitle").value = rule?.title || ""; $("ruleAmount").value = rule?.amount ?? ""; $("ruleDay").value = rule?.day_of_month ?? "";
  $("ruleKind").value = rule?.kind || "other"; $("ruleIsPayday").checked = Boolean(rule?.is_payday);
  $("ruleActive").checked = rule?.active !== 0 && rule?.active !== false;
  $("ruleEffectiveDate").value = todayISO();
  $("ruleBillCategory").value = rule?.category_id || "";
  $("ruleDialog").showModal();
}
$("addIncomeRuleBtn").addEventListener("click", () => openRule("income"));
$("addBillRuleBtn").addEventListener("click", () => openRule("bill"));
window.editIncomeRule = id => openRule("income", state.bootstrap.income_rules.find(x => x.id === id));
window.editBillRule = id => openRule("bill", state.bootstrap.bill_rules.find(x => x.id === id));
window.deleteIncomeRule = async id => { if(!confirm("Удалить доход?")) return; await api(`/api/income-rules/${id}`, {method:"DELETE"}); await loadAll(); };
window.deleteBillRule = async id => { if(!confirm("Удалить платеж?")) return; await api(`/api/bill-rules/${id}`, {method:"DELETE"}); await loadAll(); };

$("ruleForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = $("ruleMode").value, id = $("ruleId").value;
  let body;
  if (mode === "income") body = { title:$("ruleTitle").value, amount:moneyValue("ruleAmount"), day_of_month:Number($("ruleDay").value), kind:$("ruleKind").value, is_payday:$("ruleIsPayday").checked, active:$("ruleActive").checked, effective_date:$("ruleEffectiveDate").value };
  else body = { title:$("ruleTitle").value, amount:moneyValue("ruleAmount"), day_of_month:Number($("ruleDay").value), category_id:$("ruleBillCategory").value ? Number($("ruleBillCategory").value) : null, active:$("ruleActive").checked, effective_date:$("ruleEffectiveDate").value };
  const base = mode === "income" ? "/api/income-rules" : "/api/bill-rules";
  try { await api(id ? `${base}/${id}` : base, {method:id?"PUT":"POST", body:JSON.stringify(body)}); $("ruleDialog").close(); toast("Сохранено"); await loadAll(); } catch(err){ toast(err.message); }
});

function generalSettingsPayload() {
  return {
    currency: $("currency").value,
    // The legacy reserve is no longer the cash-flow start capital.  Preserve
    // it here; the dedicated button below saves the visible start amount.
    initial_reserve: Number(state.bootstrap?.settings?.initial_reserve || 0),
    forecast_months: Number($("forecastMonths").value || 4),
  };
}

function payrollSettingsPayload() {
  return {
    effective_date: $("payrollEffectiveDate").value,
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

$("savePayrollBtn").addEventListener("click", async () => {
  try {
    await api("/api/payroll-settings", {method:"PUT", body:JSON.stringify(payrollSettingsPayload())});
    toast("Расчёт зарплаты сохранён");
    await loadAll();
  } catch(e) { toast(e.message); }
});

$("addCategoryBtn").addEventListener("click", async () => {
  const title = prompt("Название категории"); if (!title) return;
  const emoji = prompt("Эмодзи", "💳") || "💳";
  try { await api("/api/categories", {method:"POST", body:JSON.stringify({title,emoji})}); await loadAll(); } catch(e){ toast(e.message); }
});

window.addEventListener("DOMContentLoaded", () => loadAll(), { once: true });
