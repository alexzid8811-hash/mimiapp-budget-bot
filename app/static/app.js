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
  $("reserveReason").textContent = d.reserve.reason;
  const pct = d.period_budget > 0 ? Math.min(100, Math.max(0, d.spent / d.period_budget * 100)) : 0;
  $("dayProgress").style.width = `${pct}%`;
  const warn = $("deficitWarning");
  if (d.deficit > 0) { warn.textContent = `Не хватает ${money(d.deficit)} даже после доступного резерва. Проверьте будущие платежи или доходы.`; warn.classList.remove("hidden"); }
  else warn.classList.add("hidden");
  $("forecastList").innerHTML = d.forecast.length ? d.forecast.map(p => `
    <div class="list-row"><div class="row-text"><div class="row-title">${fmtDate(p.start)} — ${fmtDate(p.end)}</div><div class="row-sub">Доход ${money(p.income)} · платежи ${money(p.mandatory)}</div></div><div class="amount ${p.net < 0 ? 'expense':'income'}">${p.net >= 0 ? '+' : ''}${money(p.net)}</div></div>`).join("") : `<div class="empty">Добавьте суммы зарплаты и аванса, чтобы увидеть прогноз.</div>`;
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
  return `<div class="list-row"><div class="row-main"><div class="emoji">${escapeHtml(t.category_emoji) || (isExpense ? '💳':'💰')}</div><div class="row-text"><div class="row-title">${escapeHtml(title)}</div><div class="row-sub">${fmtDate(t.tx_date)}${t.category_title ? ` · ${escapeHtml(t.category_title)}`:''}</div></div></div><div><div class="amount ${t.type}">${isExpense?'-':'+'}${money(t.amount)}</div><div class="actions">${editPayment}<button class="tiny danger" onclick="deleteTx(${t.id})">Удалить</button></div></div></div>`;
}

function renderTransactions() {
  const el = $("transactionsList");
  if (!state.transactions.length) { el.innerHTML = `<div class="empty">Пока нет операций.</div>`; return; }

  const weeks = new Map();
  state.transactions.forEach(t => {
    const weekStart = transactionWeekStart(t.tx_date);
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
      const countLabel = transactions.length === 1 ? "1 операция"
        : transactions.length < 5 ? `${transactions.length} операции`
        : `${transactions.length} операций`;
      return `<section class="transaction-week ${collapsed ? "collapsed" : ""}">
        <button class="transaction-week-toggle" type="button" onclick="toggleTransactionWeek('${weekStart}')" aria-expanded="${!collapsed}">
          <span><strong>${transactionWeekTitle(weekStart)}</strong><small>${countLabel}</small></span>
          <span class="transaction-week-summary">${expenseTotal ? `−${money(expenseTotal)}` : ""}<i aria-hidden="true">⌄</i></span>
        </button>
        <div class="transaction-week-rows">${transactions.map(renderTransactionRow).join("")}</div>
      </section>`;
    }).join("");
}

function renderPlan() {
  const el = $("planList");
  if (!state.plan.length) { el.innerHTML = `<div class="empty">В этом периоде обязательных платежей нет.</div>`; return; }
  el.innerHTML = state.plan.map(p => {
    const planned = Number(p.planned_amount ?? p.amount);
    const paidCaption = Number(p.amount) === planned
      ? 'Оплачено'
      : `Оплачено из ${money(planned)}`;
    const remainderCaption = Number(p.remainder_amount || 0) > 0
      ? ` · ${money(p.remainder_amount)} в копилке`
      : '';
    const action = p.paid
      ? `<div class="bill-payment-action"><button class="tiny" onclick="editBillPayment(${p.payment_id})">Изменить</button><div class="bill-paid-caption">${paidCaption}${remainderCaption}</div></div>`
      : `<div class="bill-payment-action"><button class="tiny" onclick="payBill(${p.id},'${p.due_date}')">Оплачено</button></div>`;
    return `<div class="list-row bill-row ${p.paid?'paid':''}"><div class="row-main"><div class="emoji">${escapeHtml(p.category_emoji) || '📌'}</div><div class="row-text"><div class="bill-heading"><div class="row-title">${escapeHtml(p.title)}</div><div class="amount expense">${money(p.amount)}</div></div><div class="row-sub bill-meta">${fmtDate(p.due_date)} · уже учтено в бюджете</div></div></div>${action}</div>`;
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

  $("billRulesList").innerHTML = b.bill_rules.length ? b.bill_rules.map(r => `<div class="list-row"><div class="row-text"><div class="row-title">${escapeHtml(r.title)} · ${r.day_of_month} числа</div><div class="row-sub">ежемесячно</div></div><div><div class="amount expense">${money(r.amount)}</div><div class="actions"><button class="tiny" onclick="editBillRule(${r.id})">Изм.</button><button class="tiny danger" onclick="deleteBillRule(${r.id})">×</button></div></div></div>`).join("") : `<div class="empty">Добавьте аренду, кредиты, подписки и другие обязательные платежи.</div>`;
  renderCategories();
}

function renderCategories() {
  const categories = state.bootstrap?.categories || [];
  $("categoriesList").innerHTML = categories.map(category => `
    <div class="category-order-item" data-category-id="${category.id}">
      <span class="category-order-name">${escapeHtml(category.emoji)} ${escapeHtml(category.title)}</span>
      <button class="category-drag-handle" type="button" aria-label="Перетащить ${escapeHtml(category.title)}" title="Зажмите и перетащите">☰</button>
    </div>`).join("");
}

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
function beginCategoryDrag(target, inputId) {
  if (categoryDrag) return false;
  const handle = target.closest('.category-drag-handle');
  if (!handle) return;
  const item = handle.closest('.category-order-item');
  if (!item) return;
  categoryDrag = {
    inputId,
    item,
    previous: [...(state.bootstrap?.categories || [])],
  };
  item.classList.add('dragging');
  categoriesList.classList.add('sorting');
  return true;
}

function moveCategoryDrag(clientY, inputId) {
  if (!categoryDrag || categoryDrag.inputId !== inputId) return false;
  const others = [...categoriesList.querySelectorAll('.category-order-item:not(.dragging)')];
  const before = others.find(item => {
    const rect = item.getBoundingClientRect();
    return clientY < rect.top + rect.height / 2;
  });
  categoriesList.insertBefore(categoryDrag.item, before || null);
  if (clientY < 90) window.scrollBy(0, -12);
  else if (clientY > window.innerHeight - 90) window.scrollBy(0, 12);
  return true;
}

async function finishCategoryDrag(inputId, cancelled = false) {
  if (!categoryDrag || categoryDrag.inputId !== inputId) return;
  const drag = categoryDrag;
  categoryDrag = null;
  drag.item.classList.remove('dragging');
  categoriesList.classList.remove('sorting');
  if (cancelled) {
    renderCategories();
    return;
  }
  const categoryIds = categoryIdsFromList(categoriesList);
  if (categoryIds.every((id, index) => id === drag.previous[index]?.id)) return;
  await persistCategoryOrder(drag.previous, categoryIds);
}

categoriesList.addEventListener('pointerdown', event => {
  // Touch is handled below through the iOS-compatible touch event path.
  if (event.pointerType === 'touch') return;
  const inputId = `pointer:${event.pointerId}`;
  if (!beginCategoryDrag(event.target, inputId)) return;
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
  if (!touch || !beginCategoryDrag(event.target, `touch:${touch.identifier}`)) return;
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
    $("incomeTxDestination").value = transaction.income_destination || "daily";
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
    await api(path, { method:editingTransactionId ? "PUT" : "POST", body: JSON.stringify({ type:"income", amount:moneyValue("incomeTxAmount"), tx_date:$("incomeTxDate").value, category_id:null, note:$("incomeTxNote").value, income_destination:$("incomeTxDestination").value }) });
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
  $("ruleTitle").value = rule?.title || ""; $("ruleAmount").value = rule?.amount ?? ""; $("ruleDay").value = rule?.day_of_month ?? "";
  $("ruleKind").value = rule?.kind || "other"; $("ruleIsPayday").checked = Boolean(rule?.is_payday);
  $("ruleActive").checked = rule?.active !== 0 && rule?.active !== false;
  $("ruleEffectiveDate").value = todayISO();
  $("ruleBillCategory").value = rule?.category_id || "";
  $("ruleDialog").showModal();
}
$("addBillRuleBtn").addEventListener("click", () => openRule("bill"));
window.editBillRule = id => openRule("bill", state.bootstrap.bill_rules.find(x => x.id === id));
window.deleteBillRule = async id => { if(!confirm("Удалить платеж?")) return; try { await api(`/api/bill-rules/${id}`, {method:"DELETE"}); await loadAll(); } catch(e) { toast(e.message); } };

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

$("addCategoryBtn").addEventListener("click", async () => {
  const title = prompt("Название категории"); if (!title) return;
  const emoji = prompt("Эмодзи", "💳") || "💳";
  try { await api("/api/categories", {method:"POST", body:JSON.stringify({title,emoji})}); await loadAll(); } catch(e){ toast(e.message); }
});

window.addEventListener("DOMContentLoaded", () => loadAll(), { once: true });
