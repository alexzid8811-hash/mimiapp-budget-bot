(() => {
  const tgApp = window.Telegram?.WebApp;
  const requestHeaders = { "Content-Type": "application/json" };
  if (tgApp?.initData) requestHeaders["X-Telegram-Init-Data"] = tgApp.initData;

  async function request(path, options = {}) {
    const res = await fetch(path, { ...options, headers: { ...requestHeaders, ...(options.headers || {}) } });
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
    return new Date().toISOString().slice(0, 10);
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
    setText("reserveBalance", formatMoney(flow.buffer_balance));
    setText("reserveTarget", `на счету ${formatMoney(flow.current_cash)}`);

    let reason = flow.reason || "";
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

  async function refreshCashflow() {
    try {
      const [settings, flow] = await Promise.all([
        request("/api/cashflow-settings"),
        request("/api/cashflow"),
      ]);

      const enabled = document.getElementById("cashflowEnabled");
      const startDate = document.getElementById("cashflowStartDate");
      if (enabled) enabled.checked = Boolean(settings.cashflow_enabled);
      if (startDate && document.activeElement !== startDate) startDate.value = settings.start_date || todayISO();

      applyCashflow(flow);
    } catch (err) {
      console.warn("Cashflow refresh failed", err);
    }
  }

  const saveButton = document.getElementById("saveCashflowBtn");
  if (saveButton) {
    saveButton.addEventListener("click", async () => {
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
        if (typeof toast === "function") toast("Стартовый капитал и буфер сохранены");
        if (typeof loadAll === "function") await loadAll();
        else await refreshCashflow();
      } catch (err) {
        if (typeof toast === "function") toast(err.message);
      }
    });
  }

  // Wrap the app refresh so cash-flow numbers are reapplied after every
  // expense, income, vacation or settings change.
  if (typeof loadAll === "function") {
    const originalLoadAll = loadAll;
    loadAll = async function (...args) {
      const result = await originalLoadAll(...args);
      await refreshCashflow();
      return result;
    };
  }

  setTimeout(refreshCashflow, 350);
  setTimeout(refreshCashflow, 1200);
})();
