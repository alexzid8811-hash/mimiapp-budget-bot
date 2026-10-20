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

  function requestErrorMessage(body, status) {
    const detail = body?.detail;
    if (Array.isArray(detail)) {
      return detail.map(item => item.msg || item.message || JSON.stringify(item)).join("; ");
    }
    if (detail && typeof detail === "object") {
      return detail.msg || detail.message || JSON.stringify(detail);
    }
    return detail || `Ошибка ${status}`;
  }

  async function request(path, options = {}) {
    const res = await fetch(path, { ...options, headers: currentRequestHeaders(options.headers) });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(requestErrorMessage(body, res.status));
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
      minimumFractionDigits: 2,
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
    const saveCap = document.getElementById("saveDailyCapBtn");
    if (saveCap && !saveCap.dataset?.bound) {
      saveCap.addEventListener("click", saveDailyCap);
      if (saveCap.dataset) saveCap.dataset.bound = "true";
    }
    const saveFloor = document.getElementById("saveBufferFloorBtn");
    if (saveFloor && !saveFloor.dataset?.bound) {
      saveFloor.addEventListener("click", saveBufferFloor);
      if (saveFloor.dataset) saveFloor.dataset.bound = "true";
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

    const piggyBalance = Number(flow.piggy_bank_balance || 0);
    // The physical card holds only the spending money for this period.
    // Buffer and piggy-bank money are kept on a separate account.
    setText("cardCash", formatMoney(flow.remaining_period));
    setText("cardBuffer", formatMoney(flow.buffer_balance));
    setText("cardPiggy", formatMoney(piggyBalance));
    setText("cardReservesTotal", formatMoney(Number(flow.buffer_balance || 0) + piggyBalance));

    let reason = flow.reason || "";
    if (Number(flow.reserved_mandatory || 0) > 0) {
      reason += ` На обязательные платежи зарезервировано ${formatMoney(flow.reserved_mandatory)}.`;
    }
    if (flow.daily_cap != null) {
      reason += ` Максимум в день — ${formatMoney(flow.daily_cap)}.`;
      const kept = Number(flow.period_cap_kept || 0);
      const toPiggy = Number(flow.period_cap_kept_piggy || 0);
      if (toPiggy > 0) reason += ` Сверх него в этом периоде в копилку ушло ${formatMoney(toPiggy)}.`;
      if (kept - toPiggy > 0.004) {
        reason += ` Сверх него в этом периоде в буфере осталось ${formatMoney(kept - toPiggy)}.`;
      }
    }
    if (Number(flow.yearly_saved || 0) > 0) {
      reason += ` На годовые платежи накоплено ${formatMoney(flow.yearly_saved)}.`;
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
    const cap = document.getElementById("dailyCap");
    if (cap && document.activeElement !== cap) cap.value = settings.daily_cap ?? "";
    const capTo = document.getElementById("dailyCapTo");
    if (capTo) capTo.value = settings.daily_cap_to === "piggy" ? "piggy" : "buffer";
    const floor = document.getElementById("bufferFloor");
    if (floor && document.activeElement !== floor) floor.value = Number(settings.buffer_floor) > 0 ? settings.buffer_floor : "";
  };

  async function saveBufferFloor() {
    try {
      const raw = (document.getElementById("bufferFloor")?.value || "").trim();
      const value = raw ? parseMoneyField("bufferFloor") : 0;
      const settings = await request("/api/buffer-floor", {
        method: "PUT",
        body: JSON.stringify({ amount: value > 0 ? value : null }),
      });
      if (typeof toast === "function") {
        toast(Number(settings.buffer_floor) > 0
          ? `Минимум буфера ${formatMoney(settings.buffer_floor)}, бюджет пересчитан`
          : "Минимум буфера снят, бюджет пересчитан");
      }
      await loadAll();
    } catch (err) {
      if (typeof toast === "function") toast(err.message);
    }
  }

  async function saveDailyCap() {
    try {
      const raw = (document.getElementById("dailyCap")?.value || "").trim();
      const value = raw ? parseMoneyField("dailyCap") : 0;
      const settings = await request("/api/daily-cap", {
        method: "PUT",
        body: JSON.stringify({
          amount: value > 0 ? value : null,
          excess_to: document.getElementById("dailyCapTo")?.value === "piggy" ? "piggy" : "buffer",
        }),
      });
      if (typeof toast === "function") {
        toast(settings.daily_cap != null
          ? `Максимум в день ${formatMoney(settings.daily_cap)}, избыток — ${settings.daily_cap_to === "piggy" ? "в копилку" : "в буфер"}`
          : "Максимум в день снят, бюджет пересчитан");
      }
      await loadAll();
    } catch (err) {
      if (typeof toast === "function") toast(err.message);
    }
  }

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
