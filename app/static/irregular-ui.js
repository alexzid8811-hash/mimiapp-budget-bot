// Irregular-income mode ("Подработки"): home screen, income dialog with a
// live split preview, emergency reserve, bills fund, settings and income
// analytics.  All numbers come from /api/irregular*; nothing is computed here.
(() => {
  const $ = id => document.getElementById(id);
  const money = v => new Intl.NumberFormat("ru-RU", {
    style: "currency", currency: "RUB", minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(Number(v || 0));
  const day = s => new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" }).format(new Date(`${s}T12:00:00`));
  const ddmm = s => `${s.slice(8, 10)}.${s.slice(5, 7)}`;
  const esc = s => String(s ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
  const plural = n => n % 100 >= 11 && n % 100 <= 14 ? "дней" : n % 10 === 1 ? "день" : n % 10 >= 2 && n % 10 <= 4 ? "дня" : "дней";
  const today = () => window.budgetDate.today();
  const parseMoney = v => window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(v) : Number(v) || 0;
  const notify = message => { if (typeof toast === "function") toast(message); };
  const setText = (id, value) => { const el = $(id); if (el) el.textContent = value; };
  const show = (id, visible) => $(id)?.classList.toggle("hidden", !visible);

  async function request(path, options = {}) {
    if (typeof api === "function") return api(path, options);
    const res = await fetch(path, { ...options, headers: { "Content-Type": "application/json" } });
    if (!res.ok) throw new Error(`Ошибка ${res.status}`);
    return res.json();
  }

  let current = { state: null, flow: null, reserve: null, pendingMode: false };
  const isIrregular = () => current.state?.bootstrap?.settings?.budget_mode === "irregular";

  function applyModeClass() {
    const on = isIrregular() || current.pendingMode;
    document.body?.classList?.toggle("mode-irregular", on);
    document.querySelectorAll("[data-budget-mode]").forEach(b => {
      const active = String(b.dataset.budgetMode === (on ? "irregular" : "payroll"));
      b.setAttribute("aria-pressed", active);
      b.setAttribute("aria-selected", active); // .segmented highlights the selected button
    });
  }

  // --- Home -------------------------------------------------------------
  function renderHome(flow) {
    const target = Number(flow.today_target) || 0;
    const left = Number(flow.available_today) || 0;
    const spent = Number(flow.spent_today) || 0;
    setText("dailyAvailable", money(target));
    setText("leftToday", money(left));
    setText("spentToday", money(spent));
    $("todayHero")?.classList.toggle("over", left < 0);
    const pct = target > 0 ? Math.min(100, Math.max(0, spent / target * 100)) : spent > 0 ? 100 : 0;
    if ($("spendFill")) $("spendFill").style.width = `${pct}%`;
    const overspend = Number(flow.overspend) || 0;
    show("overspendPanel", overspend > 0);
    if (overspend > 0) setText("overspendAmount", money(overspend));
    setText("periodCaption", `Растягиваем до ${day(flow.stretch_until)} · ${flow.days_left} ${plural(flow.days_left)}`);

    setText("irrFree", money(flow.free_balance));
    setText("irrStretch", `Растягиваем до ${ddmm(flow.stretch_until)} (осталось ${flow.days_left} дн.)`);
    setText("irrReserve", money(flow.reserve_balance));
    setText("irrReserveDays", flow.reserve_days != null
      ? `хватит примерно на ${flow.reserve_days} ${plural(flow.reserve_days)} при средних тратах` : "");
    setText("irrBills", money(flow.bills_reserved));
    const bill = flow.next_bill;
    setText("irrNextBill", bill ? `ближайший: ${bill.title}, ${day(bill.due_date)} — ${money(bill.amount)}` : "ближайших платежей нет");
    setText("irrNoIncome", String(flow.days_without_income));
    setText("irrLastIncome", flow.last_income_date ? `последний доход ${day(flow.last_income_date)}` : "доходов ещё не было");

    const shortfall = flow.bills_shortfall;
    setText("irrBillsWarning", shortfall ? `На обязательные не хватает ${money(shortfall.amount)} к ${ddmm(shortfall.date)}` : "");
    show("irrBillsWarning", Boolean(shortfall));
    setText("irrNoIncomeText", `Дохода не было ${flow.days_without_income} ${plural(flow.days_without_income)}. `);
    show("irrNoIncomeWarning", Boolean(flow.no_income_warning));
  }

  // --- Reserves -----------------------------------------------------------
  function renderObligations(flow) {
    setText("irrObligationsTotal", money(flow.bills_reserved));
    const shortfall = flow.bills_shortfall;
    setText("irrObligationsWarning", shortfall ? `Не хватает ${money(shortfall.amount)} к ${ddmm(shortfall.date)}` : "");
    show("irrObligationsWarning", Boolean(shortfall));
    const list = $("irrObligationsList");
    if (!list) return;
    const rows = flow.bills || [];
    list.innerHTML = rows.length ? rows.map(b => {
      const status = b.paid ? `оплачено ${money(b.paid_amount)}`
        : Number(b.missing) > 0 ? `отложено ${money(b.reserved)} · не хватает ${money(b.missing)}`
        : `отложено полностью`;
      return `<div class="list-row"><div class="row-text"><div class="row-title">${esc(b.title)}</div><div class="row-sub">${day(b.due_date)} · ${status}</div></div><div class="amount ${b.paid ? "" : "expense"}">${money(b.amount)}</div></div>`;
    }).join("") : `<div class="empty">Обязательных платежей в окне нет.</div>`;
  }

  function renderReserve(reserve, flow) {
    setText("irrReserveBalance", money(reserve.balance));
    const list = $("irrReserveMovements");
    if (!list) return;
    const items = reserve.movements || [];
    list.innerHTML = items.length ? items.map(m => {
      const plus = Number(m.amount) >= 0;
      const remove = m.id && !m.expense_id
        ? `<div class="actions"><button class="tiny danger" type="button" onclick="irregularUi.deleteMovement(${m.id})">Удалить</button></div>` : "";
      return `<div class="list-row"><div class="row-main"><div class="movement-icon ${plus ? "deposit" : "withdraw"}">${plus ? "+" : "−"}</div><div class="row-text"><div class="row-title">${esc(m.title)}</div><div class="row-sub">${day(m.date)}${m.reason ? ` · ${esc(m.reason)}` : ""} · остаток ${money(m.balance)}</div></div></div><div><div class="amount ${plus ? "income" : "expense"}">${plus ? "+" : "−"}${money(Math.abs(m.amount))}</div>${remove}</div></div>`;
    }).join("") : `<div class="empty">В резерве пока нет движений.</div>`;
    const withdraw = $("irrWithdrawBtn");
    if (withdraw) withdraw.disabled = Number(reserve.balance || 0) <= 0;
    if (flow && $("irrReserveNote") && flow.reserve_days != null) {
      setText("irrReserveNote", `При средних тратах ${money(flow.avg_daily_spending)} в день резерва хватит примерно на ${flow.reserve_days} ${plural(flow.reserve_days)}.`);
    }
  }

  // --- Settings -------------------------------------------------------------
  function renderSettings(settings) {
    if (!settings) return;
    const set = (id, value) => { const el = $(id); if (el && document.activeElement !== el) el.value = value ?? ""; };
    set("irrPercent", settings.reserve_percent ?? 10);
    set("irrTarget", settings.reserve_target ?? "");
    set("irrStretchDays", settings.stretch_days ?? 14);
    set("irrLookahead", settings.lookahead_days ?? 30);
    set("irrStartDate", settings.start_date || today());
    set("irrStartTotal", settings.start_total ?? 0);
    set("irrStartReserve", settings.start_reserve ?? 0);
  }

  function settingsPayload() {
    const target = String($("irrTarget")?.value || "").trim();
    return {
      reserve_percent: Number(String($("irrPercent").value || "10").replace(",", ".")),
      reserve_target: target ? parseMoney(target) || null : null,
      stretch_days: Number($("irrStretchDays").value || 14),
      lookahead_days: Number($("irrLookahead").value === "" ? 30 : $("irrLookahead").value),
      start_date: $("irrStartDate").value || today(),
      start_total: parseMoney($("irrStartTotal").value),
      start_reserve: parseMoney($("irrStartReserve").value),
    };
  }

  async function saveSettings() {
    try {
      const body = settingsPayload();
      await request("/api/irregular/settings", { method: "PUT", body: JSON.stringify(body) });
      if (!isIrregular()) {
        await request("/api/budget-mode", { method: "PUT", body: JSON.stringify({ budget_mode: "irregular", start_date: body.start_date, start_total: body.start_total, start_reserve: body.start_reserve }) });
      }
      current.pendingMode = false;
      notify(isIrregular() ? "Настройки подработок сохранены" : "Режим подработок включён");
      await loadAll();
    } catch (e) { notify(e.message); }
  }

  async function chooseMode(mode) {
    if (mode === "irregular" && !current.flow?.configured) {
      // The start must be entered first: show the card with its fields.
      current.pendingMode = true;
      applyModeClass();
      renderSettings(current.flow?.settings || current.state?.irregular?.settings || {});
      notify("Укажите старт режима и нажмите «Сохранить»");
      return;
    }
    current.pendingMode = false;
    try {
      await request("/api/budget-mode", { method: "PUT", body: JSON.stringify({ budget_mode: mode }) });
      notify(mode === "irregular" ? "Режим: подработки" : "Режим: зарплата и аванс");
      await loadAll();
    } catch (e) { notify(e.message); }
  }

  // --- Income dialog --------------------------------------------------------
  let previewTimer = null;
  let previewId = 0;

  function incomeBody() {
    const percent = String($("irrIncomePercent").value || "").replace(",", ".");
    return {
      amount: parseMoney($("irrIncomeAmount").value),
      tx_date: $("irrIncomeDate").value || today(),
      income_source: $("irrIncomeSource").value.trim(),
      reserve_percent: percent === "" ? null : Number(percent),
      destination: $("irrIncomeDestination").value || "split",
      note: $("irrIncomeNote").value,
    };
  }

  function previewMarkup(p) {
    const part = (label, value, cls) => Number(value) > 0 ? `<div class="irr-part ${cls}"><span>${label}</span><b>${money(value)}</b></div>` : "";
    return `<div class="irr-preview-parts">${part("В резерв", p.reserve, "reserve")}${part("На обязательные", p.bills, "bills")}${part("Свободные", p.free, "free")}${part("В копилку", p.piggy, "piggy")}</div><div class="muted">${esc(p.summary)}${Number(p.free) > 0 ? ` · растягиваем до ${ddmm(p.stretch_until)}` : ""}</div>`;
  }

  async function preview() {
    const body = incomeBody();
    const box = $("irrIncomePreview");
    if (!box) return null;
    if (!(body.amount > 0)) { box.innerHTML = `<div class="muted">Введите сумму — здесь появится разбивка.</div>`; return null; }
    const id = ++previewId;
    const exclude = Number($("irrIncomeId").value) || null;
    try {
      const result = await request("/api/irregular/preview", { method: "POST", body: JSON.stringify({
        amount: body.amount, tx_date: body.tx_date, reserve_percent: body.reserve_percent,
        destination: body.destination, exclude_id: exclude,
      }) });
      if (id !== previewId) return null;
      box.innerHTML = previewMarkup(result);
      return result;
    } catch (e) {
      if (id === previewId) box.innerHTML = `<div class="warning">${esc(e.message)}</div>`;
      return null;
    }
  }

  function schedulePreview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(preview, 250);
  }

  function openIncome(transaction = null) {
    const settings = current.flow?.settings || {};
    $("irrIncomeId").value = transaction?.id || "";
    setText("irrIncomeTitle", transaction ? "Изменить доход" : "Новый доход");
    $("irrIncomeAmount").value = transaction?.amount ?? "";
    $("irrIncomeDate").value = transaction?.tx_date || today();
    $("irrIncomeSource").value = transaction?.income_source || "";
    $("irrIncomePercent").value = transaction ? (transaction.reserve_percent ?? 0) : (settings.reserve_percent ?? 10);
    const destination = transaction?.income_destination;
    $("irrIncomeDestination").value = ["piggy", "reserve"].includes(destination) ? destination : "split";
    $("irrIncomeNote").value = transaction?.note || "";
    const sources = [...new Set((current.state?.transactions || []).map(t => t.income_source).filter(Boolean))];
    if ($("irrSources")) $("irrSources").innerHTML = sources.map(s => `<option value="${esc(s)}"></option>`).join("");
    if ($("irrIncomePreview")) $("irrIncomePreview").innerHTML = "";
    if (transaction) preview();
    $("irrIncomeDialog").showModal();
    setTimeout(() => $("irrIncomeAmount")?.focus?.(), 50);
  }

  async function submitIncome(event) {
    event?.preventDefault?.();
    const id = $("irrIncomeId").value;
    try {
      const saved = await request(id ? `/api/irregular/incomes/${id}` : "/api/irregular/incomes", {
        method: id ? "PUT" : "POST", body: JSON.stringify(incomeBody()),
      });
      $("irrIncomeDialog").close();
      notify(saved?.summary || "Доход записан");
      await loadAll();
    } catch (e) { notify(e.message); }
  }

  // --- Reserve dialog -------------------------------------------------------
  function openReserve(mode, purpose = "pay_expense") {
    $("irrReserveMode").value = mode;
    const withdraw = mode === "withdraw";
    setText("irrReserveTitle", withdraw ? "Взять из резерва" : mode === "return" ? "Вернуть в резерв" : "Пополнить резерв");
    show("irrPurposeField", withdraw);
    show("irrKindField", false);
    $("irrReservePurpose").value = purpose;
    $("irrReserveKind").value = mode === "return" ? "from_free" : "deposit";
    const cats = current.state?.bootstrap?.categories || [];
    $("irrReserveCategory").innerHTML = cats.map(c => `<option value="${c.id}">${esc(c.emoji)} ${esc(c.title)}</option>`).join("");
    $("irrReserveAmount").value = purpose === "cover_overspend" ? (current.flow?.overspend || "") : "";
    $("irrReserveDate").value = today();
    $("irrReserveReason").value = "";
    updateReserveForm();
    $("irrReserveDialog").showModal();
  }

  function updateReserveForm() {
    const mode = $("irrReserveMode").value;
    const purpose = $("irrReservePurpose").value;
    const withdraw = mode === "withdraw";
    show("irrCategoryField", withdraw && purpose === "pay_expense");
    const todayOnly = (withdraw && purpose !== "pay_expense") || mode === "return";
    const date = $("irrReserveDate");
    if (date) {
      date.disabled = todayOnly;
      // A date picked for «Оплатить расход» must not stay behind the lock.
      if (todayOnly) date.value = today();
    }
    const reason = $("irrReserveReason");
    if (reason) reason.required = withdraw;
    const flow = current.flow || {};
    setText("irrReserveHint", !withdraw
      ? (mode === "return" ? `Свободных денег сейчас ${money(flow.free_balance)}.` : "Деньги извне сразу попадают в резерв.")
      : purpose === "pay_expense" ? "Создаётся расход этой категории. Дневной лимит не меняется."
      : purpose === "to_free" ? `Сумма разделится на дни до ${flow.stretch_until ? ddmm(flow.stretch_until) : "даты растягивания"}.`
      : `Не больше сегодняшнего перерасхода: ${money(flow.overspend)}.`);
  }

  async function submitReserve(event) {
    event?.preventDefault?.();
    const mode = $("irrReserveMode").value;
    const amount = parseMoney($("irrReserveAmount").value);
    const movement_date = $("irrReserveDate").value || today();
    const reason = $("irrReserveReason").value.trim();
    try {
      if (mode === "withdraw") {
        const purpose = $("irrReservePurpose").value;
        if (!reason) throw new Error("Укажите причину");
        await request("/api/irregular/reserve/withdraw", { method: "POST", body: JSON.stringify({
          amount, movement_date, purpose, reason,
          category_id: purpose === "pay_expense" ? Number($("irrReserveCategory").value) : null,
        }) });
      } else {
        await request("/api/irregular/reserve/deposit", { method: "POST", body: JSON.stringify({
          amount, movement_date, reason, kind: mode === "return" ? "from_free" : "deposit",
        }) });
      }
      $("irrReserveDialog").close();
      notify(mode === "withdraw" ? "Взято из резерва" : "Резерв пополнен");
      await loadAll();
    } catch (e) { notify(e.message); }
  }

  async function deleteMovement(id) {
    if (!confirm("Удалить операцию резерва?")) return;
    try {
      await request(`/api/irregular/reserve/movements/${id}`, { method: "DELETE" });
      await loadAll();
    } catch (e) { notify(e.message); }
  }

  // --- Income analytics -------------------------------------------------------
  let analyticsAnchor = null;

  function bars(items, label) {
    const max = Math.max(0, ...items.map(i => Number(i.amount) || 0));
    return `<div class="irr-bars">${items.map(i => `<div class="irr-bar" title="${esc(label(i))}: ${money(i.amount)}"><span style="height:${max > 0 ? Math.max(2, Number(i.amount) / max * 100) : 2}%"></span><small>${esc(label(i))}</small></div>`).join("")}</div>`;
  }

  function renderAnalytics(data) {
    setText("irrAnalyticsLabel", data.label);
    setText("irrAnalyticsTotal", money(data.total));
    if ($("irrAnalyticsNext")) $("irrAnalyticsNext").disabled = !data.next_anchor;
    const stat = (title, s) => s ? `<div><span>${title}</span><b>${money(s.average)}</b><small>медиана ${money(s.median)}${s.months < Number(title.match(/\d+/)[0]) ? ` · по ${s.months} мес.` : ""}</small></div>` : `<div><span>${title}</span><b>—</b><small>мало истории</small></div>`;
    const gap = data.longest_gap;
    $("irrAnalyticsStats").innerHTML = [
      stat("Средний доход за 3 мес.", data.stats["3"]),
      stat("Средний доход за 6 мес.", data.stats["6"]),
      `<div><span>Между поступлениями</span><b>${data.median_interval_days != null ? `${data.median_interval_days} ${plural(data.median_interval_days)}` : "—"}</b><small>медиана</small></div>`,
      `<div><span>Дольше всего без дохода</span><b>${gap ? `${gap.days} ${plural(gap.days)}` : "—"}</b><small>${gap ? `${day(gap.from)} — ${day(gap.to)}` : ""}</small></div>`,
    ].join("");
    $("irrAnalyticsWeeks").innerHTML = bars(data.weeks, w => day(w.start));
    $("irrAnalyticsMonths").innerHTML = bars(data.months, m => m.label.slice(0, 3));
    $("irrAnalyticsSources").innerHTML = data.sources.length ? data.sources.map(s => `<div class="list-row compact-row"><div class="row-text"><div class="row-title">${esc(s.title)}</div><div class="row-sub">${s.count} пост. · ${Math.round(s.share * 100)}%</div></div><div class="amount income">${money(s.amount)}</div></div>`).join("") : `<div class="empty">Доходов за месяц нет.</div>`;
    const r = data.reserve;
    const taken = r.taken_items.map(t => `<div class="list-row compact-row"><div class="row-text"><div class="row-title">${esc(t.title)}</div><div class="row-sub">${day(t.date)}${t.reason ? ` · ${esc(t.reason)}` : ""}</div></div><div class="amount expense">−${money(t.amount)}</div></div>`).join("");
    $("irrAnalyticsReserve").innerHTML = `<div class="preview-line"><span>Отложено</span><strong>${money(r.put_aside)}</strong></div><div class="preview-line"><span>Взято</span><strong>${money(r.taken)}</strong></div>${taken}`;
  }

  async function loadAnalytics(anchor = analyticsAnchor) {
    if (!isIrregular()) return;
    analyticsAnchor = anchor;
    try {
      const params = new URLSearchParams({ mode: "month" });
      if (anchor) params.set("anchor", anchor);
      renderAnalytics(await request(`/api/analytics/income?${params}`));
    } catch (e) { notify(e.message); }
  }

  // --- Render entry point ----------------------------------------------------
  async function render(state) {
    current.state = state;
    current.flow = state?.irregular || null;
    applyModeClass();
    renderSettings(current.flow?.settings);
    if (!isIrregular() || !current.flow?.configured) return;
    renderHome(current.flow);
    renderObligations(current.flow);
    try {
      current.reserve = await request("/api/irregular/reserve");
      renderReserve(current.reserve, current.flow);
    } catch (e) { notify(e.message); }
  }

  document.querySelectorAll("[data-budget-mode]").forEach(b => b.addEventListener("click", () => chooseMode(b.dataset.budgetMode)));
  $("saveIrregularBtn")?.addEventListener("click", saveSettings);
  $("irrIncomeForm")?.addEventListener("submit", submitIncome);
  ["irrIncomeAmount", "irrIncomeDate", "irrIncomePercent", "irrIncomeDestination"].forEach(id => {
    $(id)?.addEventListener("input", schedulePreview);
  });
  $("irrIncomeDestination")?.addEventListener("change", schedulePreview);
  $("navAddIncome")?.addEventListener("click", () => openIncome());
  $("irrReserveForm")?.addEventListener("submit", submitReserve);
  $("irrReservePurpose")?.addEventListener("change", updateReserveForm);
  $("irrWithdrawBtn")?.addEventListener("click", () => openReserve("withdraw"));
  $("irrDepositBtn")?.addEventListener("click", () => openReserve("deposit"));
  $("irrReturnBtn")?.addEventListener("click", () => openReserve("return"));
  $("irrTakeReserveBtn")?.addEventListener("click", () => openReserve("withdraw", "to_free"));
  $("coverFromReserveBtn")?.addEventListener("click", () => openReserve("withdraw", "cover_overspend"));
  $("irrAnalyticsPrev")?.addEventListener("click", () => {
    const start = analyticsAnchor || today();
    loadAnalytics(window.budgetDate.shift(`${start.slice(0, 7)}-01`, -1));
  });
  $("irrAnalyticsNext")?.addEventListener("click", () => {
    const start = analyticsAnchor || today();
    loadAnalytics(window.budgetDate.shift(`${start.slice(0, 7)}-28`, 7));
  });
  document.querySelector('[data-nav="analytics"]')?.addEventListener("click", () => loadAnalytics());

  window.irregularUi = {
    render, openIncome, preview, submitIncome, openReserve, submitReserve, deleteMovement,
    chooseMode, saveSettings, loadAnalytics, renderAnalytics,
  };
})();
