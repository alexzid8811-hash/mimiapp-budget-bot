const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

let state = { bootstrap: null, dashboard: null, transactions: [], plan: [] };
const $ = (id) => document.getElementById(id);
const initData = tg?.initData || "";
const headers = { "Content-Type": "application/json" };
if (initData) headers["X-Telegram-Init-Data"] = initData;

function toast(message) {
  const el = $("toast"); el.textContent = message; el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 1800);
}

async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: { ...headers, ...(options.headers || {}) } });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Ошибка ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

function currency() { return state.bootstrap?.settings?.currency || "RUB"; }
function money(v) {
  return new Intl.NumberFormat("ru-RU", { style: "currency", currency: currency(), maximumFractionDigits: 0 }).format(Number(v || 0));
}
function fmtDate(s) { return new Intl.DateTimeFormat("ru-RU", { day:"numeric", month:"short" }).format(new Date(`${s}T12:00:00`)); }
function todayISO() { return new Date().toISOString().slice(0,10); }

async function loadAll() {
  try {
    const [bootstrap, dashboard, transactions, plan] = await Promise.all([
      api("/api/bootstrap"), api("/api/dashboard"), api("/api/transactions"), api("/api/plan")
    ]);
    state = { bootstrap, dashboard, transactions, plan };
    render();
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
  $("remaining").textContent = money(d.remaining);
  $("periodBudget").textContent = money(d.period_budget);
  $("mandatory").textContent = money(d.mandatory);
  $("reserveBalance").textContent = money(d.reserve.balance);
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
    return `<div class="list-row"><div class="row-main"><div class="emoji">${t.category_emoji || (isExpense ? '💳':'💰')}</div><div class="row-text"><div class="row-title">${escapeHtml(title)}</div><div class="row-sub">${fmtDate(t.tx_date)}${t.category_title ? ` · ${escapeHtml(t.category_title)}`:''}</div></div></div><div><div class="amount ${t.type}">${isExpense?'-':'+'}${money(t.amount)}</div><div class="actions"><button class="tiny danger" onclick="deleteTx(${t.id})">Удалить</button></div></div></div>`;
  }).join("");
}

function renderPlan() {
  const el = $("planList");
  if (!state.plan.length) { el.innerHTML = `<div class="empty">В этом периоде обязательных платежей нет.</div>`; return; }
  el.innerHTML = state.plan.map(p => `<div class="list-row ${p.paid?'paid':''}"><div class="row-main"><div class="emoji">${p.category_emoji || '📌'}</div><div class="row-text"><div class="row-title">${escapeHtml(p.title)}</div><div class="row-sub">${fmtDate(p.due_date)} · уже учтено в бюджете</div></div></div><div><div class="amount expense">${money(p.amount)}</div>${p.paid ? '<div class="row-sub">оплачено</div>' : `<button class="tiny" onclick="payBill(${p.id},'${p.due_date}')">Оплачено</button>`}</div></div>`).join("");
}

function renderSettings() {
  const b = state.bootstrap; if (!b) return;
  $("initialReserve").value = b.settings.initial_reserve ?? 0;
  $("forecastMonths").value = b.settings.forecast_months ?? 4;
  $("currency").value = b.settings.currency || "RUB";
  $("incomeRulesList").innerHTML = b.income_rules.map(r => `<div class="list-row"><div class="row-text"><div class="row-title">${escapeHtml(r.title)} · ${r.day_of_month} числа</div><div class="row-sub">${r.is_payday ? 'граница периода · ' : ''}${r.active ? 'активно' : 'выключено'}</div></div><div><div class="amount income">${money(r.amount)}</div><div class="actions"><button class="tiny" onclick="editIncomeRule(${r.id})">Изм.</button><button class="tiny danger" onclick="deleteIncomeRule(${r.id})">×</button></div></div></div>`).join("");
  $("billRulesList").innerHTML = b.bill_rules.length ? b.bill_rules.map(r => `<div class="list-row"><div class="row-text"><div class="row-title">${escapeHtml(r.title)} · ${r.day_of_month} числа</div><div class="row-sub">ежемесячно</div></div><div><div class="amount expense">${money(r.amount)}</div><div class="actions"><button class="tiny" onclick="editBillRule(${r.id})">Изм.</button><button class="tiny danger" onclick="deleteBillRule(${r.id})">×</button></div></div></div>`).join("") : `<div class="empty">Добавьте аренду, кредиты, подписки и другие обязательные платежи.</div>`;
  $("categoriesList").innerHTML = b.categories.map(c => `<span class="chip">${c.emoji} ${escapeHtml(c.title)}</span>`).join("");
}

function fillCategorySelects() {
  const cats = state.bootstrap?.categories || [];
  const options = cats.map(c => `<option value="${c.id}">${c.emoji} ${escapeHtml(c.title)}</option>`).join("");
  $("expenseCategory").innerHTML = options;
  $("ruleBillCategory").innerHTML = `<option value="">Без категории</option>${options}`;
}

function escapeHtml(s) { return String(s ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }

function switchPage(page) {
  document.querySelectorAll('.page').forEach(x => x.classList.toggle('active', x.dataset.page === page));
  document.querySelectorAll('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.nav === page));
  window.scrollTo({top:0, behavior:'smooth'});
}

document.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', () => switchPage(btn.dataset.nav)));
$("refreshBtn").addEventListener("click", loadAll);
$("quickExpenseBtn").addEventListener("click", () => { $("expenseDate").value = todayISO(); $("expenseDialog").showModal(); setTimeout(() => $("expenseAmount").focus(), 50); });
$("addIncomeTxBtn").addEventListener("click", () => { $("incomeTxDate").value = todayISO(); $("incomeTxDialog").showModal(); });

$("expenseForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/transactions", { method:"POST", body: JSON.stringify({ type:"expense", amount:Number($("expenseAmount").value), tx_date:$("expenseDate").value, category_id:Number($("expenseCategory").value), note:$("expenseNote").value }) });
    $("expenseDialog").close(); e.target.reset(); toast("Расход записан"); await loadAll();
  } catch(e2) { toast(e2.message); }
});

$("incomeTxForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/transactions", { method:"POST", body: JSON.stringify({ type:"income", amount:Number($("incomeTxAmount").value), tx_date:$("incomeTxDate").value, category_id:null, note:$("incomeTxNote").value }) });
    $("incomeTxDialog").close(); e.target.reset(); toast("Доход записан"); await loadAll();
  } catch(e2) { toast(e2.message); }
});

window.deleteTx = async (id) => { if (!confirm("Удалить операцию?")) return; await api(`/api/transactions/${id}`, {method:"DELETE"}); await loadAll(); };
window.payBill = async (id, due) => { try { await api(`/api/bills/${id}/pay`, {method:"POST", body:JSON.stringify({due_date:due})}); toast("Отмечено оплачено"); await loadAll(); } catch(e){ toast(e.message); } };

function openRule(mode, rule = null) {
  $("ruleMode").value = mode; $("ruleId").value = rule?.id || "";
  const income = mode === "income";
  $("ruleDialogTitle").textContent = rule ? "Изменить правило" : (income ? "Новый доход" : "Новый платеж");
  $("incomeRuleFields").style.display = income ? "block" : "none";
  $("billCategoryWrap").style.display = income ? "none" : "flex";
  $("ruleTitle").value = rule?.title || ""; $("ruleAmount").value = rule?.amount ?? ""; $("ruleDay").value = rule?.day_of_month ?? "";
  $("ruleKind").value = rule?.kind || "other"; $("ruleIsPayday").checked = Boolean(rule?.is_payday);
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
  if (mode === "income") body = { title:$("ruleTitle").value, amount:Number($("ruleAmount").value), day_of_month:Number($("ruleDay").value), kind:$("ruleKind").value, is_payday:$("ruleIsPayday").checked, active:true };
  else body = { title:$("ruleTitle").value, amount:Number($("ruleAmount").value), day_of_month:Number($("ruleDay").value), category_id:$("ruleBillCategory").value ? Number($("ruleBillCategory").value) : null, active:true };
  const base = mode === "income" ? "/api/income-rules" : "/api/bill-rules";
  try { await api(id ? `${base}/${id}` : base, {method:id?"PUT":"POST", body:JSON.stringify(body)}); $("ruleDialog").close(); toast("Сохранено"); await loadAll(); } catch(err){ toast(err.message); }
});

$("saveSettingsBtn").addEventListener("click", async () => {
  try { await api("/api/settings", {method:"PUT", body:JSON.stringify({currency:$("currency").value, initial_reserve:Number($("initialReserve").value||0), forecast_months:Number($("forecastMonths").value||4)})}); toast("Настройки сохранены"); await loadAll(); } catch(e){ toast(e.message); }
});

$("addCategoryBtn").addEventListener("click", async () => {
  const title = prompt("Название категории"); if (!title) return;
  const emoji = prompt("Эмодзи", "💳") || "💳";
  try { await api("/api/categories", {method:"POST", body:JSON.stringify({title,emoji})}); await loadAll(); } catch(e){ toast(e.message); }
});

loadAll();
