(() => {
  if (window.__cashflowUiLoaded) return;
  window.__cashflowUiLoaded = true;
  function currentTelegramInitData() {
    if (typeof window.getTelegramInitData === "function") {
      const sharedInitData = window.getTelegramInitData();
      if (sharedInitData) return sharedInitData;
    }

    const sdkInitData = window.Telegram?.WebApp?.initData;
    if (sdkInitData) return sdkInitData;

    for (const source of [window.location.hash.slice(1), window.location.search.slice(1)]) {
      const urlInitData = new URLSearchParams(source).get("tgWebAppData");
      if (urlInitData) return urlInitData;
    }
    return "";
  }

  function currentRequestHeaders(extraHeaders = {}) {
    const headers = { "Content-Type": "application/json", ...extraHeaders };
    const initData = currentTelegramInitData();
    if (initData) headers["X-Telegram-Init-Data"] = initData;
    return headers;
  }

  async function request(path, options = {}) {
    const res = await fetch(path, { ...options, headers: currentRequestHeaders(options.headers) });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Ошибка ${res.status}`);
    }
    return res.json();
  }

  function parseMoneyField(id) {
    const value = document.getElementById(id)?.value || "0";
    return window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(value) : Number(value) || 0;
  }

  function formatMoney(value) {
    const currency = document.getElementById("currency")?.value || "RUB";
    return new Intl.NumberFormat("ru-RU", {
      style: "currency",
      currency,
      minimumFractionDigits: 0,
      maximumFractionDigits: 2,
    }).format(Number(value || 0));
  }

  function todayISO() {
    return window.budgetDate.today();
  }

  function injectSettingsUI() {
    const save = document.getElementById("saveCashflowBtn");
    if (save && !save.dataset?.bound) {
      save.addEventListener("click", saveCashflowSettings);
      if (save.dataset) save.dataset.bound = "true";
    }
  }

  function applyCashflow(flow) {
    if (!flow?.enabled) return;

    const setText = (id, value) => {
      const el = document.getElementById(id);
      if (el) el.textContent = value;
    };

    setText("periodBudget", formatMoney(flow.period_budget));
    setText("dailyAvailable", formatMoney(flow.available_today));
    setText("remaining", formatMoney(flow.remaining_period));
    setText("mandatory", formatMoney(flow.mandatory_period));
    setText("reserveBalance", formatMoney(flow.buffer_balance));
    setText("reserveTarget", `на счету ${formatMoney(flow.current_cash)}`);

    let reason = flow.reason || "";
    if (Number(flow.reserved_mandatory || 0) > 0) {
      reason += ` На обязательные платежи зарезервировано ${formatMoney(flow.reserved_mandatory)}.`;
    }
    if (flow.next_income) {
      const d = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" })
        .format(new Date(`${flow.next_income.date}T12:00:00`));
      reason += ` Следующее поступление: ${d} — ${formatMoney(flow.next_income.income)}.`;
    }
    setText("reserveReason", reason.trim());

    const warning = document.getElementById("deficitWarning");
    if (warning) {
      if (Number(flow.capital_shortfall || 0) > 0) {
        warning.textContent = `Чтобы не уйти в минус даже без ежедневных трат, не хватает ${formatMoney(flow.capital_shortfall)}.`;
        warning.classList.remove("hidden");
      } else {
        warning.classList.add("hidden");
      }
    }

    const progress = document.getElementById("dayProgress");
    if (progress) {
      const budget = Number(flow.period_budget || 0);
      const spent = Number(flow.spent_period || 0);
      const pct = budget > 0 ? Math.min(100, Math.max(0, spent / budget * 100)) : 0;
      progress.style.width = `${pct}%`;
    }
  }

  window.applyCashflow = applyCashflow;
  window.applyCashflowSettings = settings => {
    injectSettingsUI();
    document.getElementById("cashflowEnabled").checked = Boolean(settings.cashflow_enabled);
    document.getElementById("cashflowStartDate").value = settings.start_date || todayISO();
    document.getElementById("initialReserve").value = settings.start_capital ?? 0;
  };

  async function saveCashflowSettings() {
    try {
      const startDate = document.getElementById("cashflowStartDate")?.value || todayISO();
      await request("/api/cashflow-settings", {
        method: "PUT",
        body: JSON.stringify({
          cashflow_enabled: Boolean(document.getElementById("cashflowEnabled")?.checked),
          start_date: startDate,
          start_capital: parseMoneyField("initialReserve"),
        }),
      });
      if (typeof toast === "function") toast("Стартовый капитал сохранён");
      await loadAll();
    } catch (err) {
      if (typeof toast === "function") toast(err.message);
    }
  }

  injectSettingsUI();
})();
