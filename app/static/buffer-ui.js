(() => {
  let bufferState = { buffer: null, piggy: { balance: 0, movements: [] } };

  function initData() {
    if (typeof getTelegramInitData === "function") return getTelegramInitData();
    return window.Telegram?.WebApp?.initData || "";
  }

  async function request(path, options = {}) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    const value = initData();
    if (value) headers["X-Telegram-Init-Data"] = value;
    const response = await fetch(path, { ...options, headers });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `Ошибка ${response.status}`);
    }
    return response.json();
  }

  function formatMoney(value) {
    const code = document.getElementById("currency")?.value || "RUB";
    return new Intl.NumberFormat("ru-RU", {
      style: "currency", currency: code, maximumFractionDigits: 0,
    }).format(Number(value || 0));
  }

  function formatDate(value) {
    return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" })
      .format(new Date(`${value}T12:00:00`));
  }

  function safe(value) {
    if (typeof escapeHtml === "function") return escapeHtml(value);
    return String(value ?? "").replace(/[&<>'"]/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[char]);
  }

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = value;
  }

  function renderBuffer() {
    const data = bufferState.buffer;
    if (!data) return;
    const disabled = document.getElementById("bufferDisabled");
    disabled?.classList.toggle("hidden", Boolean(data.enabled));
    setText("bufferHorizon", data.enabled ? `до ${formatDate(data.horizon_end)}` : "выключен");
    setText("bufferPageBalance", data.enabled ? formatMoney(data.buffer_balance) : "—");
    setText("bufferPageDaily", data.enabled ? formatMoney(data.available_today) : "—");

    const shortfall = document.getElementById("bufferShortfall");
    if (data.enabled && Number(data.capital_shortfall || 0) > 0) {
      shortfall.textContent = `Даже без повседневных трат не хватает ${formatMoney(data.capital_shortfall)}. Увеличьте стартовый капитал или скорректируйте обязательные платежи.`;
      shortfall.classList.remove("hidden");
    } else {
      shortfall.classList.add("hidden");
    }

    const body = document.getElementById("bufferPeriods");
    if (!data.enabled || !data.periods?.length) {
      const message = data.enabled ? "Нет периодов для прогноза." : "План появится после включения расчёта в настройках.";
      body.innerHTML = `<tr><td colspan="10"><div class="empty">${message}</div></td></tr>`;
      return;
    }

    const labels = ["Дата", "Вид", "Получено", "Дней", "Обязательные", "Свободно", "В день", "Отложить", "Взять", "Буфер"];
    body.innerHTML = data.periods.map((period, index) => {
      const cells = [
        `${formatDate(period.start)} — ${formatDate(period.end)}`,
        safe(period.kind),
        formatMoney(period.received),
        period.days,
        formatMoney(period.mandatory),
        formatMoney(period.free),
        formatMoney(period.daily),
        period.put_aside > 0 ? formatMoney(period.put_aside) : "—",
        period.take > 0 ? formatMoney(period.take) : "—",
        formatMoney(period.buffer),
      ];
      return `<tr class="${index === 0 ? "current-row" : ""}">${cells.map((cell, i) => {
        const color = i === 7 && period.put_aside > 0 ? "positive" : i === 8 && period.take > 0 ? "negative" : "";
        return `<td data-label="${labels[i]}" class="${color}">${cell}</td>`;
      }).join("")}</tr>`;
    }).join("");
  }

  function renderPiggy() {
    const piggy = bufferState.piggy || { balance: 0, movements: [] };
    setText("piggyBalance", formatMoney(piggy.balance));
    const withdraw = document.getElementById("piggyWithdrawBtn");
    if (withdraw) withdraw.disabled = Number(piggy.balance || 0) <= 0;
    const list = document.getElementById("piggyMovements");
    if (!piggy.movements?.length) {
      list.innerHTML = '<div class="empty">В копилке пока нет операций.</div>';
      return;
    }
    list.innerHTML = piggy.movements.map(item => {
      const deposit = item.direction === "deposit";
      return `<div class="list-row"><div class="row-main"><div class="movement-icon ${item.direction}">${deposit ? "+" : "−"}</div><div class="row-text"><div class="row-title">${deposit ? "Пополнение" : "Снятие"}</div><div class="row-sub">${formatDate(item.movement_date)}${item.note ? ` · ${safe(item.note)}` : ""}</div></div></div><div><div class="amount ${deposit ? "income" : "expense"}">${deposit ? "+" : "−"}${formatMoney(item.amount)}</div><div class="actions"><button class="tiny danger" type="button" onclick="deletePiggyMovement(${item.id})">Удалить</button></div></div></div>`;
    }).join("");
  }

  async function refresh() {
    try {
      const [buffer, piggy] = await Promise.all([
        request("/api/buffer"), request("/api/piggy-bank"),
      ]);
      bufferState = { buffer, piggy };
      renderBuffer();
      renderPiggy();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  }

  function openMovement(direction) {
    document.getElementById("piggyDirection").value = direction;
    document.getElementById("piggyDialogTitle").textContent =
      direction === "deposit" ? "Пополнить копилку" : "Снять из копилки";
    document.getElementById("savePiggyBtn").textContent =
      direction === "deposit" ? "Пополнить" : "Снять";
    document.getElementById("piggyAmount").value = "";
    document.getElementById("piggyDate").value = new Date().toISOString().slice(0, 10);
    document.getElementById("piggyNote").value = "";
    document.getElementById("piggyDialog").showModal();
  }

  document.getElementById("piggyDepositBtn")?.addEventListener("click", () => openMovement("deposit"));
  document.getElementById("piggyWithdrawBtn")?.addEventListener("click", () => openMovement("withdraw"));
  document.getElementById("piggyForm")?.addEventListener("submit", async event => {
    event.preventDefault();
    const direction = document.getElementById("piggyDirection").value;
    const raw = document.getElementById("piggyAmount").value;
    const amount = window.ruMoneyInput?.parseMoney ? window.ruMoneyInput.parseMoney(raw) : Number(raw);
    try {
      await request(`/api/piggy-bank/${direction}`, {
        method: "POST",
        body: JSON.stringify({
          amount,
          movement_date: document.getElementById("piggyDate").value,
          note: document.getElementById("piggyNote").value,
        }),
      });
      document.getElementById("piggyDialog").close();
      if (typeof toast === "function") {
        toast(direction === "deposit" ? "Копилка пополнена" : "Деньги возвращены в бюджет");
      }
      await refresh();
      if (typeof loadAll === "function") await loadAll();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  });

  window.deletePiggyMovement = async id => {
    if (!confirm("Удалить операцию копилки?")) return;
    try {
      await request(`/api/piggy-bank/movements/${id}`, { method: "DELETE" });
      await refresh();
      if (typeof loadAll === "function") await loadAll();
    } catch (error) {
      if (typeof toast === "function") toast(error.message);
    }
  };

  document.querySelector('[data-nav="buffer"]')?.addEventListener("click", refresh);
  setTimeout(refresh, 450);
})();
